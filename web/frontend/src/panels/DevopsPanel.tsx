import React, { useEffect, useState } from 'react'
/**
 * DevOps console: live logs, the delivery pipeline, recent errors and the
 * maintenance actions. The largest tab by far, which is why the shell never pays
 * for it.
 */

import { Activity, Cable, Check, Copy, Database, Download, HeartPulse, RefreshCw, Terminal, Upload, X } from 'lucide-react'
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
    isRtl,
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
              <div>
                <div style={{ marginBottom: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12 }}>
                  <div>
                    <h2 style={{ fontSize: '1.25rem', fontWeight: 800, letterSpacing: '-0.02em', display: 'flex', alignItems: 'center', gap: 10 }}>
                      <Terminal size={22} color="#818cf8" />
                      <span>{t('ui_pro_active_logger_diagnostics')}</span>
                    </h2>
                    <p style={{ fontSize: '0.825rem', color: '#94a3b8', marginTop: 4 }}>
                      {t('ui_high_throughput_independent_logger_microservice_on_grpc_6002_wit')}
                    </p>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <button
                      onClick={() => setLogAutoRefresh(!logAutoRefresh)}
                      style={{
                        padding: '6px 14px',
                        borderRadius: 8,
                        border: logAutoRefresh ? '1px solid rgba(16, 185, 129, 0.4)' : '1px solid rgba(255, 255, 255, 0.1)',
                        background: logAutoRefresh ? 'rgba(16, 185, 129, 0.15)' : 'rgba(15, 23, 42, 0.6)',
                        color: logAutoRefresh ? '#34d399' : '#94a3b8',
                        fontSize: '0.8rem',
                        fontWeight: 600,
                        cursor: 'pointer',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                      }}
                    >
                      <span
                        style={{
                          width: 8,
                          height: 8,
                          borderRadius: '50%',
                          background: logAutoRefresh ? '#10b981' : 'var(--color-text-muted)',
                          boxShadow: logAutoRefresh ? '0 0 8px #10b981' : 'none',
                        }}
                      />
                      <span>{logAutoRefresh ? (t('ui_live_stream_active_3s')) : (t('ui_live_stream_paused'))}</span>
                    </button>

                    <button
                      onClick={() => fetchLogs()}
                      className="btn btn-secondary"
                      style={{ padding: '6px 12px', fontSize: '0.8rem' }}
                      title={t('ui_refresh_now')}
                    >
                      <RefreshCw size={14} />
                      <span>{t('ui_refresh')}</span>
                    </button>
                  </div>
                </div>

                {/* Microservices Topology — live probed health + connection map */}
                {(() => {
                  const hmap: Record<string, { online: boolean; detail: string; latency_ms: number } | undefined> = {}
                  for (const s of servicesHealth?.services || []) hmap[s.name] = s
                  const nodes = [
                    { key: 'web', title: t('ui_web_gateway'), sub: 'atf-web · :8088', color: '#f59e0b' },
                    { key: 'api', title: t('ui_rest_control_plane'), sub: 'atf-api · :8080', color: '#a855f7' },
                    { key: 'core', title: t('ui_python_core_mtproto'), sub: 'atf-core · gRPC :50051', color: '#38bdf8' },
                    { key: 'logger', title: t('ui_central_logger'), sub: 'atf-logger · gRPC :50052', color: '#34d399' },
                  ]
                  const svc = (k: string) => hmap[k]
                  return (
                    <div className="glass-panel" style={{ padding: 18, marginBottom: 20 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 10, marginBottom: 14 }}>
                        <h3 style={{ fontSize: '1rem', fontWeight: 700, display: 'flex', alignItems: 'center', gap: 8 }}>
                          <HeartPulse size={17} color={servicesHealth?.status === 'healthy' ? '#34d399' : '#ef4444'} />
                          <span>{t('ui_services_topology_live_health')}</span>
                        </h3>
                        <span
                          className="badge"
                          style={{
                            background: servicesHealth?.status === 'healthy' ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)',
                            color: servicesHealth?.status === 'healthy' ? '#34d399' : '#f87171',
                            fontSize: 'var(--type-xs)',
                            fontWeight: 700,
                          }}
                        >
                          {servicesHealth
                            ? servicesHealth.status === 'healthy'
                              ? t('ui_all_services_healthy')
                              : t('ui_degraded')
                            : t('ui_probing')}
                        </span>
                      </div>

                      {/* Node chain: Web → API → Core → Logger, connection arrows labelled */}
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
                        {nodes.map((n, i) => {
                          const s = svc(n.key)
                          const online = s?.online ?? false
                          return (
                            <React.Fragment key={n.key}>
                              {i > 0 && (
                                <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '2px 0 2px 22px', height: 26 }} dir="ltr">
                                  <div style={{ width: 2, height: '100%', minHeight: 18, background: online ? 'rgba(16,185,129,0.5)' : 'rgba(239,68,68,0.4)', borderRadius: 2 }} />
                                  <Cable size={12} color={online ? '#34d399' : '#ef4444'} />
                                  <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
                                    {i === 1
                                      ? t('ui_rest_proxy_jwt')
                                      : i === 2
                                      ? t('ui_grpc_control')
                                      : t('ui_grpc_logs')}
                                  </span>
                                </div>
                              )}
                              <div
                                style={{
                                  display: 'flex',
                                  alignItems: 'center',
                                  justifyContent: 'space-between',
                                  flexWrap: 'wrap',
                                  gap: 8,
                                  padding: '10px 14px',
                                  borderRadius: 10,
                                  border: `1px solid ${online ? 'rgba(16,185,129,0.25)' : 'rgba(239,68,68,0.35)'}`,
                                  background: online ? 'rgba(16,185,129,0.05)' : 'rgba(239,68,68,0.07)',
                                }}
                              >
                                <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                                  <span
                                    style={{
                                      width: 10, height: 10, borderRadius: '50%', flexShrink: 0,
                                      background: online ? '#10b981' : '#ef4444',
                                      boxShadow: online ? '0 0 10px #10b981' : '0 0 10px #ef4444',
                                      animation: online ? 'none' : 'none',
                                    }}
                                  />
                                  <div style={{ minWidth: 0 }}>
                                    <div style={{ fontSize: '0.85rem', fontWeight: 700, color: n.color }}>{n.title}</div>
                                    <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', fontFamily: 'monospace' }} dir="ltr">{n.sub}</div>
                                  </div>
                                </div>
                                <div style={{ textAlign: 'right', minWidth: 0, maxWidth: '100%' }} dir="ltr">
                                  <div style={{ fontSize: 'var(--type-xs)', fontWeight: 700, color: online ? '#34d399' : '#f87171' }}>
                                    {online ? (t('ui_online')) : (t('ui_down'))}
                                    {s && s.latency_ms > 0 ? ` · ${s.latency_ms}ms` : ''}
                                  </div>
                                  {s?.detail && (
                                    <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                      {s.detail}
                                    </div>
                                  )}
                                </div>
                              </div>
                            </React.Fragment>
                          )
                        })}
                      </div>

                      {/* Degraded-service hint — tell the operator which systemctl to poke */}
                      {servicesHealth && servicesHealth.status !== 'healthy' && (
                        <div style={{ marginTop: 12, padding: '8px 12px', borderRadius: 8, background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.25)', fontSize: '0.75rem', color: '#f87171' }}>
                          {t('ui_down_services')}
                          {servicesHealth.services.filter((x) => !x.online).map((x) => x.name).join(', ')}
                          <span style={{ color: '#94a3b8' }}>
                            {' — '}systemctl --user restart {servicesHealth.services.filter((x) => !x.online).map((x) => x.name === 'core' ? 'atf' : x.name === 'logger' ? 'atf-logger' : x.name === 'api' ? 'atf-api' : 'atf-web').join(' ')}
                          </span>
                        </div>
                      )}
                    </div>
                  )
                })()}

                {/* Collection policy: what each process is actually recording, and the
                    admin-only control that changes it through the core itself. */}
                <LogPolicyPanel lang={lang} />

                {/* Pro Active Log Explorer */}
                <div className="glass-panel" style={{ padding: 20 }}>
                  {/* Filter & Search Bar */}
                  <div
                    style={{
                      display: 'flex',
                      flexWrap: 'wrap',
                      gap: 12,
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      marginBottom: 16,
                      paddingBottom: 14,
                      borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
                    }}
                  >
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
                      {/* Tenant scope: admins only. Switching refetches — the
                          poller effect re-arms on `logScope` too. */}
                      {canPickScope && (
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
                      )}

                      {/* Service Filter */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{t('ui_service')}</span>
                        <select
                          value={logFilterService}
                          onChange={(e) => {
                            setLogFilterService(e.target.value)
                            fetchLogs(e.target.value, logFilterLevel, logSearch)
                          }}
                          style={{
                            background: 'rgba(15, 23, 42, 0.8)',
                            border: '1px solid rgba(255, 255, 255, 0.15)',
                            color: '#e2e8f0',
                            borderRadius: 8,
                            padding: '5px 10px',
                            fontSize: '0.8rem',
                          }}
                        >
                          <option value="all">{t('ui_all_services')}</option>
                          <option value="core">{t('ui_service_core')}</option>
                          <option value="bot">{t('ui_service_bot')}</option>
                          <option value="api">{t('ui_service_api')}</option>
                          <option value="pipeline">{t('ui_service_pipeline')}</option>
                          <option value="boot">{t('ui_service_boot')}</option>
                        </select>
                      </div>

                      {/* Level Filter */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{t('ui_level')}</span>
                        <select
                          value={logFilterLevel}
                          onChange={(e) => {
                            setLogFilterLevel(e.target.value)
                            fetchLogs(logFilterService, e.target.value, logSearch)
                          }}
                          style={{
                            background: 'rgba(15, 23, 42, 0.8)',
                            border: '1px solid rgba(255, 255, 255, 0.15)',
                            color: '#e2e8f0',
                            borderRadius: 8,
                            padding: '5px 10px',
                            fontSize: '0.8rem',
                          }}
                        >
                          <option value="all">{t('ui_all_levels')}</option>
                          <option value="ERROR">❌ {t('ui_level_error')}</option>
                          <option value="WARN">⚠️ {t('ui_level_warn')}</option>
                          <option value="INFO">ℹ️ {t('ui_level_info')}</option>
                          <option value="DEBUG">🔍 {t('ui_level_debug')}</option>
                        </select>
                      </div>

                      {/* v18: scope the console to one task's history. This is the
                          same attribution the per-task Logs tab renders, surfaced
                          where an operator already has the global view open. */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{t('ui_task')}</span>
                        <select
                          value={logFilterRule}
                          onChange={(e) => {
                            setLogFilterRule(e.target.value)
                            setLogOffset(0)
                            fetchLogs(logFilterService, logFilterLevel, logSearch, { rule: e.target.value, offset: 0 })
                          }}
                          style={{
                            background: 'rgba(15, 23, 42, 0.8)',
                            border: '1px solid rgba(255, 255, 255, 0.15)',
                            color: '#e2e8f0',
                            borderRadius: 8,
                            padding: '5px 10px',
                            fontSize: '0.8rem',
                            maxWidth: 220,
                          }}
                        >
                          <option value="all">{t('ui_all_tasks')}</option>
                          {(Array.isArray(rules) ? rules : []).map((r: any) => (
                            <option key={r.id} value={r.id}>
                              {(r.name || r.id.slice(0, 8)).slice(0, 28)}
                            </option>
                          ))}
                        </select>
                      </div>

                      {/* v18: read direction — newest-first for triage, oldest-first
                          for replaying how a task behaved. */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{t('ui_order')}</span>
                        <select
                          value={logOrder}
                          onChange={(e) => {
                            const order = e.target.value as 'desc' | 'asc'
                            setLogOrder(order)
                            setLogOffset(0)
                            fetchLogs(logFilterService, logFilterLevel, logSearch, { order, offset: 0 })
                          }}
                          style={{
                            background: 'rgba(15, 23, 42, 0.8)',
                            border: '1px solid rgba(255, 255, 255, 0.15)',
                            color: '#e2e8f0',
                            borderRadius: 8,
                            padding: '5px 10px',
                            fontSize: '0.8rem',
                          }}
                        >
                          <option value="desc">{t('ui_newest_first')}</option>
                          <option value="asc">{t('ui_oldest_first')}</option>
                        </select>
                      </div>
                    </div>

                    {/* Text Search Input — debounced; typing never re-arms the
                        poller per keystroke and never fires a fetch per key. */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 260, flex: 1, maxWidth: 380 }}>
                      <input
                        type="text"
                        placeholder={t('ui_search_logs_live')}
                        value={searchDraft}
                        onChange={(e) => setSearchDraft(e.target.value)}
                        className="input-field"
                        style={{ padding: '6px 12px', fontSize: '0.8rem' }}
                      />
                      {searchDraft && (
                        <button
                          onClick={() => setSearchDraft('')}
                          style={{
                            background: 'none',
                            border: 'none',
                            color: '#94a3b8',
                            cursor: 'pointer',
                            padding: 4,
                          }}
                        >
                          <X size={14} />
                        </button>
                      )}
                    </div>
                  </div>

                  {/* Quick Metrics Badges */}
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 14, fontSize: '0.75rem' }}>
                    <span className="badge badge-gray">
                      {logTotal === 0
                        ? (t('ui_no_events'))
                        : `${logTotal === 0 ? 0 : logOffset + 1}–${logOffset + (Array.isArray(logs) ? logs.length : 0)} / ${logTotal.toLocaleString()}`}
                    </span>
                    {logStats?.oldest_ts ? (
                      <span className="badge badge-gray" title={t('ui_oldest_record_on_disk')}>
                        {t('ui_history_from')}{' '}
                        {new Date(logStats.oldest_ts * 1000).toLocaleDateString()}
                      </span>
                    ) : null}
                    {logStats && (
                      <>
                        <span className="badge" style={{ background: 'rgba(16, 185, 129, 0.15)', color: '#34d399' }}>
                          {t('ui_level_info')}: {logStats.by_level?.INFO ?? 0}
                        </span>
                        <span className="badge" style={{ background: 'rgba(245, 158, 11, 0.15)', color: '#fbbf24' }}>
                          {t('ui_level_warn')}: {logStats.by_level?.WARN ?? 0}
                        </span>
                        <span className="badge" style={{ background: 'rgba(239, 68, 68, 0.15)', color: '#f87171' }}>
                          {t('ui_level_error')}: {logStats.by_level?.ERROR ?? 0}
                        </span>
                        <span className="badge badge-vip">
                          {t('ui_total_archive')} {logStats.total}
                        </span>
                      </>
                    )}
                  </div>

                  {/* v18 history navigation: offset paging over the full archive plus
                      a downloadable copy of exactly what is on screen. */}
                  <div style={{
                    display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center',
                    marginBottom: 14, padding: '10px 12px',
                    background: 'rgba(15, 23, 42, 0.5)', border: '1px solid rgba(255,255,255,0.06)', borderRadius: 10,
                  }}>
                    <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
                      📜 {t('ui_history')}
                    </span>
                    <button
                      onClick={() => {
                        const next = Math.max(0, logOffset - logPageSize)
                        setLogOffset(next)
                        fetchLogs(logFilterService, logFilterLevel, logSearch, { offset: next })
                      }}
                      disabled={logOffset === 0}
                      className="btn btn-secondary" style={{ padding: '5px 12px', fontSize: 'var(--type-xs)' }}
                    >
                      ⬆ {t('ui_newer')}
                    </button>
                    <button
                      onClick={() => {
                        const next = logOffset + logPageSize
                        setLogOffset(next)
                        fetchLogs(logFilterService, logFilterLevel, logSearch, { offset: next })
                      }}
                      disabled={!logHasMore}
                      className="btn btn-secondary" style={{ padding: '5px 12px', fontSize: 'var(--type-xs)' }}
                    >
                      ⬇ {t('ui_older')}
                    </button>
                    <label style={{ fontSize: 'var(--type-xs)', color: '#cbd5e1', display: 'flex', alignItems: 'center', gap: 6 }}>
                      {t('ui_page_size')}
                      <select
                        value={logPageSize}
                        onChange={(e) => {
                          const size = Number(e.target.value)
                          setLogPageSize(size)
                          setLogOffset(0)
                          fetchLogs(logFilterService, logFilterLevel, logSearch, { limit: size, offset: 0 })
                        }}
                        style={{ background: '#0b1220', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 6, padding: '4px 6px', fontSize: 'var(--type-xs)' }}
                      >
                        {[50, 100, 200, 500].map((n) => <option key={n} value={n}>{n}</option>)}
                      </select>
                    </label>
                    <span style={{ fontSize: 'var(--type-xs)', color: logHasMore ? '#fbbf24' : '#34d399' }}>
                      {logHasMore
                        ? (t('ui_older_events_exist'))
                        : (t('ui_reached_the_end_of_history'))}
                    </span>
                    <button
                      onClick={() => handleExportLogs('json')} disabled={logExporting}
                      className="btn btn-secondary" style={{ padding: '5px 12px', fontSize: 'var(--type-xs)' }}
                    >
                      {logExporting ? '⏳ ...' : '⬇ JSON'}
                    </button>
                    <button
                      onClick={() => handleExportLogs('csv')} disabled={logExporting}
                      className="btn btn-secondary" style={{ padding: '5px 12px', fontSize: 'var(--type-xs)' }}
                    >
                      {logExporting ? '⏳ ...' : '⬇ CSV'}
                    </button>
                  </div>

                  {/* Log purge controls — keeps the archive from growing unbounded. */}
                  <div style={{
                    display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center',
                    marginBottom: 14, padding: '10px 12px',
                    background: 'rgba(15, 23, 42, 0.5)', border: '1px solid rgba(255,255,255,0.06)', borderRadius: 10,
                  }}>
                    <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
                      🧹 {t('ui_purge_archive')}
                    </span>
                    <label style={{ fontSize: 'var(--type-xs)', color: '#cbd5e1', display: 'flex', alignItems: 'center', gap: 6 }}>
                      {t('ui_keep_last')}
                      <input
                        type="number" min={0} max={720} step={1} value={purgeWindowH}
                        onChange={(e) => setPurgeWindowH(Math.max(0, Math.min(720, Number(e.target.value) || 0)))}
                        style={{ width: 64, background: '#0b1220', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 6, padding: '4px 6px', fontSize: 'var(--type-xs)' }}
                      />
                      {t('ui_h')}
                    </label>
                    {(logFilterLevel !== 'all' || logFilterService !== 'all') && (
                      <span className="badge badge-gray" style={{ fontSize: 'var(--type-xs)' }}>
                        {t('ui_scoped')} {[logFilterLevel !== 'all' && logFilterLevel, logFilterService !== 'all' && logFilterService].filter(Boolean).join(' · ')}
                      </span>
                    )}
                    <button
                      onClick={handlePurgeLogs} disabled={purging}
                      className="btn btn-secondary" style={{ padding: '5px 12px', fontSize: 'var(--type-xs)' }}
                    >
                      {purging ? '⏳ ...' : (t('ui_purge_old_logs'))}
                    </button>
                    {purgeResult && (
                      <span style={{ fontSize: 'var(--type-xs)', color: '#34d399' }}>
                        ✓ {t('ui_purged')}: {purgeResult.purged} · {t('ui_left')}: {purgeResult.remaining}
                      </span>
                    )}
                  </div>

                  {/* Backup & Restore — migrate this install to another server */}
                  <div className="glass-panel" style={{ padding: 18, marginBottom: 20 }}>
                    <h3 style={{ fontSize: '1rem', fontWeight: 700, display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                      <Database size={17} color="#38bdf8" />
                      <span>{t('ui_backup_restore_server_migration')}</span>
                    </h3>
                    <p style={{ fontSize: '0.76rem', color: '#94a3b8', marginBottom: 14 }}>
                      {t('ui_every_setting_rule_filter_ai_config_and_telegram_session_is_pack')}
                    </p>

                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 14 }}>
                      {/* EXPORT */}
                      <div style={{ background: 'rgba(15, 23, 42, 0.5)', border: '1px solid rgba(56, 189, 248, 0.18)', borderRadius: 10, padding: 14 }}>
                        <div style={{ fontSize: '0.82rem', fontWeight: 700, color: '#7dd3fc', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
                          <Download size={14} /> {t('ui_1_back_up_this_server')}
                        </div>
                        <label style={{ fontSize: 'var(--type-xs)', color: '#94a3b8', display: 'block', marginBottom: 4 }}>
                          {t('ui_file_passphrase_optional_recommended')}
                        </label>
                        <input
                          type="password"
                          value={backupPassphrase}
                          onChange={(e) => setBackupPassphrase(e.target.value)}
                          placeholder={t('ui_strong_passphrase')}
                          style={{ width: '100%', boxSizing: 'border-box', background: '#0b1220', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 8, padding: '7px 10px', fontSize: '0.78rem', marginBottom: 10 }}
                        />
                        <button
                          onClick={handleBackupExport} disabled={backupBusy}
                          className="btn" style={{ width: '100%', justifyContent: 'center', padding: '8px 12px', fontSize: '0.8rem', fontWeight: 700, background: 'rgba(56, 189, 248, 0.15)', border: '1px solid rgba(56, 189, 248, 0.4)', color: '#38bdf8' }}
                        >
                          {backupBusy ? '⏳ ...' : (t('ui_download_backup'))}
                        </button>
                        {backupCounts && (
                          <div style={{ marginTop: 10, fontSize: 'var(--type-xs)', color: '#94a3b8' }}>
                            {backupEncrypted && (
                              <span className="badge" style={{ background: 'rgba(16,185,129,0.15)', color: '#34d399', fontSize: 'var(--type-xs)', marginRight: 6 }}>🔒 AES-GCM</span>
                            )}
                            {Object.entries(backupCounts).map(([k, v]) => (
                              <span key={k} style={{ display: 'inline-block', margin: '2px 4px 0 0', padding: '2px 7px', background: 'rgba(255,255,255,0.05)', borderRadius: 6 }}>
                                {k}: <b style={{ color: '#e2e8f0' }}>{v}</b>
                              </span>
                            ))}
                          </div>
                        )}
                        {!backupEncrypted && backupCounts && (
                          <div style={{ marginTop: 8, fontSize: 'var(--type-xs)', color: '#fbbf24' }}>
                            ⚠️ {t('ui_this_file_is_unencrypted_and_holds_live_credentials')}
                          </div>
                        )}
                      </div>

                      {/* IMPORT */}
                      <div style={{ background: 'rgba(15, 23, 42, 0.5)', border: '1px solid rgba(168, 85, 247, 0.18)', borderRadius: 10, padding: 14 }}>
                        <div style={{ fontSize: '0.82rem', fontWeight: 700, color: '#c4b5fd', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
                          <Upload size={14} /> {t('ui_2_restore_on_this_server')}
                        </div>
                        <label style={{ fontSize: 'var(--type-xs)', color: '#94a3b8', display: 'block', marginBottom: 4 }}>
                          {t('ui_backup_file')}
                        </label>
                        <input
                          type="file" accept=".json,.enc,application/json"
                          onChange={(e) => onBackupFileChosen(e.target.files?.[0])}
                          style={{ width: '100%', boxSizing: 'border-box', fontSize: 'var(--type-xs)', color: '#94a3b8', marginBottom: 8 }}
                        />
                        <label style={{ fontSize: 'var(--type-xs)', color: '#94a3b8', display: 'block', marginBottom: 4 }}>
                          {t('ui_or_paste_the_bundle')}
                        </label>
                        <textarea
                          value={importText}
                          onChange={(e) => { setImportText(e.target.value); setImportStats(null) }}
                          placeholder={t('ui_bundle_contents')}
                          rows={3}
                          style={{ width: '100%', boxSizing: 'border-box', background: '#0b1220', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 8, padding: '7px 10px', fontSize: 'var(--type-xs)', fontFamily: 'monospace', resize: 'vertical', marginBottom: 10 }}
                        />
                        <label style={{ fontSize: 'var(--type-xs)', color: '#94a3b8', display: 'block', marginBottom: 4 }}>
                          {t('ui_passphrase_if_the_file_is_encrypted')}
                        </label>
                        <input
                          type="password"
                          value={importPassphrase}
                          onChange={(e) => setImportPassphrase(e.target.value)}
                          placeholder={t('ui_passphrase')}
                          style={{ width: '100%', boxSizing: 'border-box', background: '#0b1220', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 8, padding: '7px 10px', fontSize: '0.78rem', marginBottom: 10 }}
                        />
                        <label style={{ fontSize: 'var(--type-xs)', color: '#cbd5e1', display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10 }}>
                          <input type="checkbox" checked={importOverwrite} onChange={(e) => setImportOverwrite(e.target.checked)} />
                          {t('ui_full_replace_overwrite_existing_rows')}
                        </label>
                        <button
                          onClick={handleBackupImport} disabled={backupBusy}
                          className="btn" style={{ width: '100%', justifyContent: 'center', padding: '8px 12px', fontSize: '0.8rem', fontWeight: 700, background: 'rgba(168, 85, 247, 0.15)', border: '1px solid rgba(168, 85, 247, 0.4)', color: '#c4b5fd' }}
                        >
                          {backupBusy ? '⏳ ...' : (t('ui_restore_backup'))}
                        </button>
                        {importStats && (
                          <div style={{ marginTop: 10, display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                            {Object.entries(importStats).map(([k, v]) => (
                              <span key={k} style={{ fontSize: 'var(--type-xs)', padding: '3px 8px', borderRadius: 6, background: 'rgba(255,255,255,0.05)', color: '#94a3b8' }}>
                                {k}: <b style={{ color: '#34d399' }}>{v.imported}</b>{v.skipped ? <span style={{ color: '#94a3b8' }}>/{v.skipped}</span> : null}{v.failed ? <span style={{ color: '#f87171' }}> ✗{v.failed}</span> : null}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Terminal Log Console */}
                  <div
                    style={{
                      background: '#070b14',
                      borderRadius: 12,
                      border: '1px solid rgba(255, 255, 255, 0.08)',
                      padding: '12px 14px',
                      fontFamily: "'JetBrains Mono', Consolas, Monaco, monospace",
                      fontSize: '0.8rem',
                      lineHeight: 1.6,
                      maxHeight: 520,
                      overflowY: 'auto',
                      boxShadow: 'inset 0 2px 10px rgba(0, 0, 0, 0.6)',
                    }}
                  >
                    {(!Array.isArray(logs) || logs.length === 0) ? (
                      <div style={{ textAlign: 'center', padding: '40px 20px', color: 'var(--color-text-muted)' }}>
                        <div style={{ fontSize: 24, marginBottom: 8 }}>📋</div>
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

                          const lvlColor = isErr ? '#ef4444' : isWarn ? '#f59e0b' : isDbg ? '#38bdf8' : '#10b981'
                          const lvlBg = isErr
                            ? 'rgba(239, 68, 68, 0.18)'
                            : isWarn
                            ? 'rgba(245, 158, 11, 0.18)'
                            : isDbg
                            ? 'rgba(56, 189, 248, 0.18)'
                            : 'rgba(16, 185, 129, 0.18)'

                          const timeStr = l.ts ? new Date(l.ts * 1000).toLocaleTimeString() : '--:--:--'
                          const logLineText = `[${timeStr}] [${lvl}] [${l.service || 'core'}/${l.category || 'sys'}] ${l.message || l.detail || ''}`

                          return (
                            <div
                              key={logId}
                              style={{
                                padding: '6px 10px',
                                borderRadius: 6,
                                background: isErr ? 'rgba(239, 68, 68, 0.06)' : 'rgba(255, 255, 255, 0.015)',
                                borderLeft: isRtl ? 'none' : `3px solid ${lvlColor}`,
                                borderRight: isRtl ? `3px solid ${lvlColor}` : 'none',
                                display: 'flex',
                                alignItems: 'flex-start',
                                justifyContent: 'space-between',
                                gap: 12,
                                wordBreak: 'break-word',
                              }}
                            >
                              <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, flex: 1 }}>
                                {/* Time */}
                                <span style={{ color: 'var(--color-text-muted)', fontSize: '0.75rem', flexShrink: 0 }}>
                                  {timeStr}
                                </span>

                                {/* Level Badge */}
                                <span
                                  style={{
                                    background: lvlBg,
                                    color: lvlColor,
                                    padding: '1px 6px',
                                    borderRadius: 4,
                                    fontWeight: 700,
                                    fontSize: 'var(--type-xs)',
                                    flexShrink: 0,
                                  }}
                                >
                                  {lvl}
                                </span>

                                {/* Service & Category */}
                                <span
                                  style={{
                                    color: '#818cf8',
                                    background: 'rgba(99, 102, 241, 0.1)',
                                    padding: '1px 6px',
                                    borderRadius: 4,
                                    fontSize: 'var(--type-xs)',
                                    flexShrink: 0,
                                  }}
                                >
                                  {l.service || 'core'}
                                  {l.category ? `:${l.category}` : ''}
                                </span>

                                {/* v18 attribution: which task (and which API call)
                                    produced this line. Clicking narrows the whole
                                    console to that subject instead of making the
                                    operator retype an id into the search box. */}
                                {l.rule_id && (
                                  <button
                                    onClick={() => {
                                      setLogFilterRule(l.rule_id as string)
                                      setLogOffset(0)
                                      fetchLogs(logFilterService, logFilterLevel, logSearch, { rule: l.rule_id as string, offset: 0 })
                                    }}
                                    title={t('ui_filter_to_this_task')}
                                    style={{
                                      color: '#f472b6',
                                      background: 'rgba(236, 72, 153, 0.14)',
                                      border: '1px solid rgba(236, 72, 153, 0.35)',
                                      padding: '1px 6px',
                                      borderRadius: 4,
                                      fontSize: 'var(--type-xs)',
                                      flexShrink: 0,
                                      cursor: 'pointer',
                                      fontFamily: 'inherit',
                                    }}
                                  >
                                    {t('ui_app_task')}{(l.rule_id as string).slice(0, 8)}
                                  </button>
                                )}
                                {l.correlation_id && (
                                  <button
                                    onClick={() => {
                                      setLogSearch(l.correlation_id as string)
                                      setLogOffset(0)
                                      fetchLogs(logFilterService, logFilterLevel, l.correlation_id as string, { offset: 0 })
                                    }}
                                    title={t('ui_trace_this_request_end_to_end')}
                                    style={{
                                      color: '#38bdf8',
                                      background: 'rgba(56, 189, 248, 0.12)',
                                      border: '1px solid rgba(56, 189, 248, 0.3)',
                                      padding: '1px 6px',
                                      borderRadius: 4,
                                      fontSize: 'var(--type-xs)',
                                      flexShrink: 0,
                                      cursor: 'pointer',
                                      fontFamily: 'inherit',
                                    }}
                                  >
                                    {t('ui_corr')}{(l.correlation_id as string).slice(0, 8)}
                                  </button>
                                )}

                                {/* Message */}
                                <span style={{ color: isErr ? '#fca5a5' : '#f1f5f9', fontWeight: isErr ? 600 : 400 }}>
                                  {l.message || l.detail}
                                </span>

                                {/* Additional Detail (if message and detail are both present) */}
                                {l.message && l.detail && l.detail !== l.message && (
                                  <div
                                    dir="ltr"
                                    style={{
                                      width: '100%',
                                      marginTop: 4,
                                      padding: '4px 8px',
                                      borderRadius: 4,
                                      background: 'rgba(0, 0, 0, 0.4)',
                                      color: '#94a3b8',
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
                                onClick={() => {
                                  navigator.clipboard.writeText(logLineText)
                                  setCopiedLogId(logId)
                                  setTimeout(() => setCopiedLogId(null), 2000)
                                }}
                                style={{
                                  background: 'none',
                                  border: 'none',
                                  color: copiedLogId === logId ? '#34d399' : 'var(--color-text-muted)',
                                  cursor: 'pointer',
                                  padding: '2px 4px',
                                  borderRadius: 4,
                                  flexShrink: 0,
                                  display: 'flex',
                                  alignItems: 'center',
                                  gap: 4,
                                  fontSize: 'var(--type-xs)',
                                }}
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

                  {/* Recent Pipeline Errors — triage queue for things to fix next */}
                  <div className="glass-panel" style={{ padding: 20, marginTop: 20 }}>
                    <h3 style={{ fontSize: '1rem', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                      <Activity size={16} color="#ef4444" />
                      <span>{t('ui_recent_pipeline_errors_triage_queue')}</span>
                      {(recentErrors || []).length > 0 && (
                        <span className="badge badge-vip" style={{ fontSize: 'var(--type-xs)' }}>{recentErrors.length}</span>
                      )}
                    </h3>

                    {(!Array.isArray(recentErrors) || recentErrors.length === 0) ? (
                      <div style={{ textAlign: 'center', padding: 26, color: 'var(--color-text-muted)', fontSize: '0.85rem' }}>
                        <div style={{ fontSize: 22, marginBottom: 6 }}>✅</div>
                        <div>{t('ui_no_pipeline_errors_recorded_all_healthy')}</div>
                      </div>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                        {recentErrors.map((err, idx) => {
                          const sev = (err.severity || 'error').toLowerCase()
                          const isErr = sev === 'error'
                          return (
                            <div
                              key={`${err.ts}-${idx}`}
                              dir="ltr"
                              style={{
                                textAlign: 'left',
                                background: 'rgba(15, 23, 42, 0.6)',
                                padding: '10px 14px',
                                borderRadius: 8,
                                fontSize: '0.78rem',
                                border: `1px solid ${isErr ? 'rgba(239, 68, 68, 0.3)' : 'rgba(245, 158, 11, 0.3)'}`,
                                display: 'flex',
                                flexDirection: 'column',
                                gap: 4,
                              }}
                            >
                              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
                                <span style={{ fontWeight: 700, color: isErr ? '#fca5a5' : '#fbbf24' }}>
                                  {err.error_name || err.category}
                                </span>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                  {err.rule_id && (
                                    <span style={{ fontSize: 'var(--type-xs)', color: '#818cf8', background: 'rgba(99,102,241,0.1)', padding: '1px 6px', borderRadius: 4 }}>
                                      {err.rule_id.substring(0, 8)}
                                    </span>
                                  )}
                                  <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
                                    {err.ts ? new Date(err.ts * 1000).toLocaleString() : ''}
                                  </span>
                                </div>
                              </div>
                              {err.detail && (
                                <div style={{ color: '#94a3b8', fontSize: 'var(--type-xs)', fontFamily: "'JetBrains Mono', Consolas, monospace", wordBreak: 'break-word' }}>
                                  {err.detail.slice(0, 260)}
                                </div>
                              )}
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                </div>
              </div>

    </>
  )
}
