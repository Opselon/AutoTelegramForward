import React, { useState, useEffect } from 'react'
import {
  Send,
  Radio,
  PlayCircle,
  Users,
  RotateCcw,
  Plus,
  Trash2,
  Edit3,
  Layers,
  Shield,
  Zap,
  Globe,
  ChevronRight,
  ChevronLeft,
  ArrowDown,
  Sparkles,
  Bot,
  Filter,
  Inbox,
  Server,
  Activity,
  Terminal,
  RefreshCw,
  Lock,
  Copy,
  Clock,
  ArrowRightLeft,
  X,
  Check,
  MessageSquare,
  HeartPulse,
  Cable,
  Database,
  Download,
  Upload,
  const [copiedLogId, setCopiedLogId] = useState<string | number | null>(null)
  const [servicesHealth, setServicesHealth] = useState<ServicesHealthResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [toast, setToast] = useState<string | null>(null)

  // Rule Modal
  const [isRuleModalOpen, setIsRuleModalOpen] = useState(false)
  const [ruleFormData, setRuleFormData] = useState<Partial<SaveRuleRequest>>({})
  // Bot-parity toggles that live inside rule metadata (block_voice, album_mode, …)
  const [ruleMeta, setRuleMeta] = useState<Record<string, any>>({})

  // AI Modal
  const [isAiModalOpen, setIsAiModalOpen] = useState(false)
  const [aiFormData, setAiFormData] = useState<Partial<SaveAIConfigRequest>>({})

  // Filter Modal
  const [isFilterModalOpen, setIsFilterModalOpen] = useState(false)
  const [filterFormData, setFilterFormData] = useState<Partial<SaveFilterRuleRequest>>({})

  // Simulator State
  const [simText, setSimText] = useState('💎 VIP SIGNAL: BUY XAUUSD @ 2655 | TP1: 2680 | SL: 2640')
  const [simOriginChatId, setSimOriginChatId] = useState('-1001111111111')
  const [simOriginTitle, setSimOriginTitle] = useState('Forex VIP Channel')
  const [simOriginUser, setSimOriginUser] = useState('vip_forex_signals')
  const [simHasMedia, setSimHasMedia] = useState(false)
  const [simIsProtected, setSimIsProtected] = useState(true)
  const [simSelectedRuleId, setSimSelectedRuleId] = useState<string>('')
  const [simResult, setSimResult] = useState<SimulateResponse | null>(null)
  const [simulating, setSimulating] = useState(false)

  // PV Assistant (AI PV Auto-Responder) State
  const [pvConfig, setPvConfig] = useState<PVResponderConfig | null>(null)
  const [pvSaving, setPvSaving] = useState(false)
  const [pvPreset, setPvPreset] = useState<'casual' | 'business' | 'short' | 'custom'>('casual')
  const [pvTestText, setPvTestText] = useState('سلام داداش، اشتراک کانال سیگنال فارکس چطوریه؟')
  const [pvTestReply, setPvTestReply] = useState<string | null>(null)
  const [pvTesting, setPvTesting] = useState(false)

  const isRtl = lang === 'fa'

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 4000)
  }

  const loadData = async () => {
    setLoading(true)
    try {
      const [r, s, ai, pt, f, q, dlq, gw, st, l, ds, pv] = await Promise.all([
        api.getRules().catch(() => []),
        api.getSessions().catch(() => []),
        api.getAIConfigs().catch(() => []),
        api.getPromptTemplates().catch(() => []),
        api.getFilters().catch(() => []),
        api.getQueueJobs().catch(() => []),
        api.getDLQJobs().catch(() => []),
        api.getGatewayInfo().catch(() => null),
        api.getStats().catch(() => null),
        api.getLogs().catch(() => []),
        api.getDeliveryStats().catch(() => null),
        api.getPVResponder().catch(() => null),
      ])
      setRules(Array.isArray(r) ? r : [])
      setSessions(Array.isArray(s) ? s : [])
      setAiConfigs(Array.isArray(ai) ? ai : [])
      setPromptTemplates(Array.isArray(pt) ? pt : [])
      setFilterRules(Array.isArray(f) ? f : [])
      setQueueJobs(Array.isArray(q) ? q : [])
      setDlqJobs(Array.isArray(dlq) ? dlq : [])
      setGatewayInfo(gw)
      setStats(st)
      setLogs(Array.isArray(l) ? l : [])
      if (pv) setPvConfig(pv)
      if (ds) {
        setDeliveryStats(ds)
        setRuleLiveStats(Array.isArray(ds.rules) ? ds.rules : [])
        setRecentErrors(Array.isArray(ds.errors) ? ds.errors : [])
        // Surface the most recent ERROR/WARN as a dismissible banner so the
        // user always sees problems instead of hunting for them in the log tab.
        setErrorBanner((() => {
          const worst = (ds.errors || []).find((e) => (e.severity || '').toLowerCase() === 'error')
            || (ds.errors || []).find((e) => (e.severity || '').toLowerCase() === 'warn')
            || null
          if (!worst) return null
          const name = worst.error_name || worst.category || ''
          let detail = worst.detail || ''
          // Input validation errors like loop detection belong in modal/bot feedback, not top banner
          if (detail.includes('loop_detected') || detail.includes('Invalid rule endpoints')) {
            return null
          }
          // Do not surface historical errors older than 30 minutes as active alert banner
          const nowTs = Math.floor(Date.now() / 1000)
          if (worst.ts && (nowTs - worst.ts > 1800)) {
            return null
          }
          // Python logs often arrive as "ValueError: ValueError <msg>" — strip the
          // duplicated exception class so the banner reads as one clean sentence.
          if (name && detail.toLowerCase().startsWith(name.toLowerCase())) {
            detail = detail.slice(name.length).replace(/^[\s:：]+/, '')
          }
          const text = (name && detail) ? `${name}: ${detail}` : (detail || name)
          const errKey = `${worst.ts || 0}_${name}_${detail}`
          const sliced = text.slice(0, 220)
          if (dismissedErrors.has(errKey) || dismissedErrors.has(text) || dismissedErrors.has(sliced)) return null
          return sliced || null
        })())
      }
      if (r.length > 0 && !simSelectedRuleId) {
        setSimSelectedRuleId(r[0].id)
      }
    } catch (e: any) {
      showToast(e.message || 'Error loading dashboard data')
    } finally {
      setLoading(false)
    }
  }

  const fetchLogs = async (srv = logFilterService, lvl = logFilterLevel, srch = logSearch) => {
    try {
      const params: any = { limit: 100 }
      if (srv !== 'all') params.service = srv
      if (lvl !== 'all') params.level = lvl
      if (srch.trim()) params.search = srch.trim()
      const [fetchedLogs, stats] = await Promise.all([
        api.getLogs(params).catch(() => []),
        api.getLogStats().catch(() => null),
      ])
      setLogs(Array.isArray(fetchedLogs) ? fetchedLogs : [])
      if (stats) setLogStats(stats)
    } catch {}
  }

  const handlePurgeLogs = async () => {
    if (purging) return
    const keepH = Math.max(0, purgeWindowH)
    const label = keepH > 0 ? `قدیمی‌تر از ${keepH} ساعت` : 'تمام لاگ‌ها (بدون حفاظت زمانی)'
    if (!window.confirm(`حذف ${logFilterLevel !== 'all' ? `سطح ${logFilterLevel} ` : ''}${logFilterService !== 'all' ? `سرویس ${logFilterService} ` : ''}${label}؟ این عمل قابل بازگشت نیست.`)) return
    setPurging(true)
    setPurgeResult(null)
    try {
      const res = await api.purgeLogs({
        level: logFilterLevel !== 'all' ? logFilterLevel : undefined,
        service: logFilterService !== 'all' ? logFilterService : undefined,
        older_than_hours: keepH,
      })
      if (res?.success) {
        setPurgeResult({ purged: res.purged, remaining: res.remaining })
        showToast(`${res.purged} لاگ حذف شد · ${res.remaining} باقی ماند`)
        await fetchLogs()
      } else {
        showToast(res?.error || 'خطا در پاکسازی لاگ‌ها')
      }
    } catch (e: any) {
      showToast(e.message || 'خطا در پاکسازی لاگ‌ها')
    } finally {
      setPurging(false)
    }
  }

  // Produce a migration bundle of everything this account owns. The bundle
  // contains live credentials; a passphrase encrypts it with AES-GCM.
  const handleBackupExport = async () => {
    if (backupBusy) return
    if (!backupPassphrase && !window.confirm('بدون رمز عبور، فایل بکاپ حاوی اطلاعات حساس به‌صورت متن ساده خواهد بود. ادامه می‌دهید؟')) return
    setBackupBusy(true)
    setBackupCounts(null)
    try {
      const res = await api.backupExport({ passphrase: backupPassphrase || undefined })
      if (res?.success) {
        setBackupCounts(res.counts || {})
        setBackupEncrypted(!!res.encrypted)
        const blob = new Blob([res.bundle], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
        a.download = `atf-backup-${stamp}${res.encrypted ? '.enc' : '.json'}`
        a.click()
        URL.revokeObjectURL(url)
        showToast(`بکاپ ساخته شد · ${Object.values(res.counts || {}).reduce((a, b) => a + b, 0)} رکورد${res.encrypted ? ' · 🔒 رمزگذاری‌شده' : ''}`)
      } else {
        showToast(res?.error || 'خطا در ساخت بکاپ')
      }
    } catch (e: any) {
      showToast(e.message || 'خطا در ساخت بکاپ')
    } finally {
      setBackupBusy(false)
    }
  }

  // Apply a bundle from this or another server. Destructive when overwrite is on.
  const handleBackupImport = async () => {
    if (backupBusy) return
    const bundle = importText.trim()
    if (!bundle) {
      showToast('متن فایل بکاپ را وارد کنید یا فایل را انتخاب کنید')
      return
    }
    const looksEncrypted = bundle.startsWith('enc:v1:')
    if (looksEncrypted && !importPassphrase) {
      showToast('این فایل رمزگذاری شده است؛ رمز عبور را وارد کنید')
      return
    }
    const totalRows = importStats ? Object.values(importStats).reduce((s, v) => s + v.imported + v.skipped + v.failed, 0) : 0
    const verb = importOverwrite ? 'جایگزینی کامل' : 'ادغام'
    if (!window.confirm(`${verb} تنظیمات از فایل بکاپ${importOverwrite ? ' (سطرهای موجود بازنویسی می‌شوند)' : ''}؟${totalRows ? ` شامل ${totalRows} رکورد.` : ''} این عمل قابل بازگشت نیست.`)) return
    setBackupBusy(true)
    setImportStats(null)
    try {
      const res = await api.backupImport({
        bundle,
        passphrase: importPassphrase || undefined,
        overwrite_existing: importOverwrite,
      })
      if (res?.success) {
        setImportStats(res.stats || {})
        const imported = Object.values(res.stats || {}).reduce((s, v) => s + v.imported, 0)
        const failed = Object.values(res.stats || {}).reduce((s, v) => s + v.failed, 0)
        showToast(`بازیابی کامل · ${imported} رکورد وارد شد${failed ? ` · ⚠️ ${failed} ناموفق` : ''}`)
        // everything the UI shows may have changed underneath it
        await loadData()
      } else {
        showToast(res?.error || 'خطا در بازیابی بکاپ')
      }
    } catch (e: any) {
      showToast(e.message || 'خطا در بازیابی بکاپ')
    } finally {
      setBackupBusy(false)
    }
  }

  const onBackupFileChosen = (file: File | undefined) => {
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      setImportText(String(reader.result || ''))
      setImportStats(null)
    }
    reader.readAsText(file)
  }

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
                <div style={{ textAlign: 'center', padding: '40px 20px', color: '#64748b' }}>
                  <div style={{ fontSize: 24, marginBottom: 8 }}>📋</div>
                  <div>{isRtl ? 'هیچ لاگی با فیلترهای جاری یافت نشد.' : 'No log events matching current filters.'}</div>
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
                          <span style={{ color: '#64748b', fontSize: '0.75rem', flexShrink: 0 }}>
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
                              fontSize: '0.7rem',
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
                              fontSize: '0.7rem',
                              flexShrink: 0,
                            }}
                          >
                            {l.service || 'core'}
                            {l.category ? `:${l.category}` : ''}
                          </span>

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
                                fontSize: '0.72rem',
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
                            color: copiedLogId === logId ? '#34d399' : '#64748b',
                            cursor: 'pointer',
                            padding: '2px 4px',
                            borderRadius: 4,
                            flexShrink: 0,
                            display: 'flex',
                            alignItems: 'center',
                            gap: 4,
                            fontSize: '0.7rem',
                          }}
                          title={isRtl ? 'کپی خط لاگ' : 'Copy log line'}
                        >
                          {copiedLogId === logId ? (
                            <>
                              <Check size={12} />
                              <span style={{ fontSize: '0.65rem' }}>{isRtl ? 'کپی شد' : 'Copied'}</span>
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
                <span>{isRtl ? 'آخرین خطاهای پایپ‌لاین (صف رفع اشکال)' : 'Recent Pipeline Errors (Triage Queue)'}</span>
                {(recentErrors || []).length > 0 && (
                  <span className="badge badge-vip" style={{ fontSize: '0.7rem' }}>{recentErrors.length}</span>
                )}
              </h3>

              {(!Array.isArray(recentErrors) || recentErrors.length === 0) ? (
                <div style={{ textAlign: 'center', padding: 26, color: '#64748b', fontSize: '0.85rem' }}>
                  <div style={{ fontSize: 22, marginBottom: 6 }}>✅</div>
                  <div>{isRtl ? 'هیچ خطایی در پایپ‌لاین ثبت نشده است.' : 'No pipeline errors recorded. All healthy.'}</div>
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
                              <span style={{ fontSize: '0.68rem', color: '#818cf8', background: 'rgba(99,102,241,0.1)', padding: '1px 6px', borderRadius: 4 }}>
                                {err.rule_id.substring(0, 8)}
                              </span>
                            )}
                            <span style={{ fontSize: '0.68rem', color: '#64748b' }}>
                              {err.ts ? new Date(err.ts * 1000).toLocaleString() : ''}
                            </span>
                          </div>
                        </div>
                        {err.detail && (
                          <div style={{ color: '#94a3b8', fontSize: '0.72rem', fontFamily: "'JetBrains Mono', Consolas, monospace", wordBreak: 'break-word' }}>
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
      )}

      {/* CREATE / EDIT RULE MODAL */}
      {isRuleModalOpen && (
        <div
          className="modal-overlay"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.8)',
            backdropFilter: 'blur(8px)',
            zIndex: 999,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 20,
          }}
        >
          <div
            className="glass-panel modal-sheet"
            style={{
              width: '100%',
              maxWidth: 680,
              maxHeight: '90vh',
              overflowY: 'auto',
              padding: 28,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20, flexShrink: 0 }}>
              <h3 style={{ fontSize: '1.2rem', fontWeight: 800 }}>
                {ruleFormData.id ? (isRtl ? 'ویرایش قانون فوروارد' : 'Edit Forward Rule') : (isRtl ? 'قانون فوروارد جدید' : 'New Forward Rule')}
              </h3>
              <button onClick={() => setIsRuleModalOpen(false)} className="btn btn-secondary icon-btn" style={{ padding: 6 }}>
                <X size={16} />
              </button>
            </div>

            <div className="modal-sheet-body">
            <form onSubmit={handleSaveRule}>
              <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {isRtl ? 'شناسه کانال مبدأ (A):' : 'Source Chat ID (A):'} *
                  </label>
                  <input
                    type="text"
                    required
                    value={ruleFormData.source_chat_id || ''}
                    onChange={(e) => setRuleFormData({ ...ruleFormData, source_chat_id: e.target.value })}
                    className="input-field"
                    placeholder="-1001111111111"
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {isRtl ? 'نام نمایشی مبدأ:' : 'Source Title:'}
                  </label>
                  <input
                    type="text"
                    value={ruleFormData.source_chat_name || ''}
                    onChange={(e) => setRuleFormData({ ...ruleFormData, source_chat_name: e.target.value })}
                    className="input-field"
                    placeholder="Forex Signals"
                  />
                </div>
              </div>

              <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {isRtl ? 'شناسه کانال مقصد (B):' : 'Target Chat ID (B):'} *
                  </label>
                  <input
                    type="text"
                    required
                    value={ruleFormData.target_chat_id || ''}
                    onChange={(e) => setRuleFormData({ ...ruleFormData, target_chat_id: e.target.value })}
                    className="input-field"
                    placeholder="-1003333333333"
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {isRtl ? 'نام نمایشی مقصد:' : 'Target Title:'}
                  </label>
                  <input
                    type="text"
                    value={ruleFormData.target_chat_name || ''}
                    onChange={(e) => setRuleFormData({ ...ruleFormData, target_chat_name: e.target.value })}
                    className="input-field"
                    placeholder="USDJPY VIP"
                  />
                </div>
              </div>

              {/* Loop Detection Real-time Warning */}
              {(() => {
                const normEp = (s?: string) => (s || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
                const s = normEp(ruleFormData.source_chat_id)
                const t = normEp(ruleFormData.target_chat_id)
                const isLoop = Boolean(s && t && s === t)
                if (!isLoop) return null
                return (
                  <div
                    style={{
                      background: 'rgba(239, 68, 68, 0.15)',
                      border: '1px solid rgba(239, 68, 68, 0.5)',
                      borderRadius: 8,
                      padding: '10px 14px',
                      marginBottom: 14,
                      color: '#fca5a5',
                      fontSize: '0.825rem',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                    }}
                  >
                    <Shield size={16} color="#ef4444" style={{ flexShrink: 0 }} />
                    <span>
                      {isRtl
                        ? '⚠️ خطای حلقه (Loop Detected): شناسه کانال مبدأ و مقصد یکسان است! فوروارد پیام به مبدأ خود مجاز نیست.'
                        : '⚠️ Routing Loop Detected: Source and target chat IDs cannot be identical! Forwarding back to source is blocked.'}
                    </span>
                  </div>
                )
              })()}

              {(() => {
                if (!ruleFormData.use_intermediate || !ruleFormData.intermediate_channel_id) return null
                const normEp = (s?: string) => (s || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
                const s = normEp(ruleFormData.source_chat_id)
                const t = normEp(ruleFormData.target_chat_id)
                const im = normEp(ruleFormData.intermediate_channel_id)
                if (im && (im === s || im === t)) {
                  return (
                    <div
                      style={{
                        background: 'rgba(239, 68, 68, 0.15)',
                        border: '1px solid rgba(239, 68, 68, 0.5)',
                        borderRadius: 8,
                        padding: '10px 14px',
                        marginBottom: 14,
                        color: '#fca5a5',
                        fontSize: '0.825rem',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 8,
                      }}
                    >
                      <Shield size={16} color="#ef4444" style={{ flexShrink: 0 }} />
                      <span>
                        {isRtl
                          ? '⚠️ خطای حلقه: کانال واسط (Hop) نمی‌تواند با کانال مبدأ یا مقصد یکسان باشد!'
                          : '⚠️ Routing Loop Detected: Intermediate hop channel cannot match source or target!'}
                      </span>
                    </div>
                  )
                }
                return null
              })()}

              {/* Hop Intermediate Settings */}
              <div
                style={{
                  background: 'rgba(15, 23, 42, 0.6)',
                  border: '1px solid rgba(245, 158, 11, 0.3)',
                  padding: 16,
                  borderRadius: 10,
                  marginBottom: 16,
                }}
              >
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', marginBottom: 12 }}>
                  <input
                    type="checkbox"
                    checked={ruleFormData.use_intermediate ?? false}
                    onChange={(e) => setRuleFormData({ ...ruleFormData, use_intermediate: e.target.checked })}
                  />
                  <span style={{ fontWeight: 700, color: '#fbbf24' }}>
                    {isRtl ? 'استفاده از هاپ برند واسط (کانال C) جهت تغییر هدر فوروارد' : 'Use Intermediate Branding Channel (Hop via C)'}
                  </span>
                </label>

                {ruleFormData.use_intermediate && (
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                        {isRtl ? 'شناسه کانال واسط C:' : 'Intermediate Channel ID (C):'}
                      </label>
                      <input
                        type="text"
                        value={ruleFormData.intermediate_channel_id || ''}
                        onChange={(e) => setRuleFormData({ ...ruleFormData, intermediate_channel_id: e.target.value })}
                        className="input-field"
                        placeholder="-1002222222222"
                      />
                    </div>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                        {isRtl ? 'نام کانال واسط C:' : 'Intermediate Title:'}
                      </label>
                      <input
                        type="text"
                        value={ruleFormData.intermediate_channel_name || ''}
                        onChange={(e) => setRuleFormData({ ...ruleFormData, intermediate_channel_name: e.target.value })}
                        className="input-field"
                        placeholder="My Brand Channel"
                      />
                    </div>
                  </div>
                )}
              </div>

              {/* Custom Header & Footer */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {isRtl ? 'هدر سفارشی پیام:' : 'Custom Header:'}
                  </label>
                  <input
                    type="text"
                    value={ruleFormData.custom_header || ''}
                    onChange={(e) => setRuleFormData({ ...ruleFormData, custom_header: e.target.value })}
                    className="input-field"
                    placeholder="💎 VIP FOREX"
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {isRtl ? 'فوتر سفارشی پیام:' : 'Custom Footer:'}
                  </label>
                  <input
                    type="text"
                    value={ruleFormData.custom_footer || ''}
                    onChange={(e) => setRuleFormData({ ...ruleFormData, custom_footer: e.target.value })}
                    className="input-field"
                    placeholder="@usdjp"
                  />
                </div>
              </div>

              {/* Bot-parity media toggles (stored in rule metadata) */}
              <div
                style={{
                  border: '1px solid rgba(99, 102, 241, 0.25)',
                  background: 'rgba(99, 102, 241, 0.06)',
                  padding: 16,
                  borderRadius: 10,
                  marginBottom: 16,
                }}
              >
                <div style={{ fontSize: '0.8rem', fontWeight: 800, color: '#a5b4fc', marginBottom: 10 }}>
                  {isRtl ? 'تنظیمات رسانه و سینک (مشابه ربات)' : 'Media & Sync Settings (bot parity)'}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 10 }}>
                  {([
                    ['block_voice', '🎙', isRtl ? 'مسدود کردن ویس' : 'Block Voice'],
                    ['block_stickers', '🎭', isRtl ? 'مسدود کردن استیکر' : 'Block Stickers'],
                    ['remove_emojis', '😀', isRtl ? 'پاکسازی ایموجی' : 'Strip Emojis'],
                    ['sync_deletes', '🗑', isRtl ? 'همگام‌سازی حذف‌ها' : 'Sync Deletes'],
                    ['ignore_edits', '✏️', isRtl ? 'نادیده گرفتن ادیت' : 'Ignore Edits'],
                  ] as const).map(([key, icon, label]) => (
                    <label
                      key={key}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer',
                        fontSize: '0.78rem', color: '#cbd5e1',
                        background: 'rgba(255,255,255,0.04)',
                        padding: '8px 10px', borderRadius: 8,
                        border: ruleMeta[key] ? '1px solid rgba(99,102,241,0.5)' : '1px solid rgba(255,255,255,0.06)',
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={!!ruleMeta[key]}
                        onChange={(e) => setRuleMeta({ ...ruleMeta, [key]: e.target.checked })}
                        style={{ accentColor: '#6366f1' }}
                      />
                      <span>{icon} {label}</span>
                    </label>
                  ))}
                </div>
                <div style={{ marginTop: 12 }}>
                  <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {isRtl ? 'حالت آلبوم:' : 'Album Mode:'}
                  </label>
                  <select
                    value={ruleMeta.album_mode || 'album'}
                    onChange={(e) => setRuleMeta({ ...ruleMeta, album_mode: e.target.value })}
                    className="input-field"
                    style={{ width: '100%' }}
                  >
                    <option value="album">{isRtl ? '🖼 آلبوم کامل' : 'Full album'}</option>
                    <option value="first">{isRtl ? '۱️⃣ فقط اولین مدیا' : 'First media only'}</option>
                    <option value="split">{isRtl ? '🔀 تفکیک پیام‌ها' : 'Split messages'}</option>
                  </select>
                </div>
              </div>

              {/* AI Rewrite (per-rule) */}
              <div style={{ marginTop: 18, paddingTop: 16, borderTop: '1px solid rgba(255,255,255,0.06)' }}>
                <div style={{ fontSize: '0.8rem', fontWeight: 800, color: '#c084fc', marginBottom: 10 }}>
                  🤖 {isRtl ? 'بازنویسی هوشمند پیام (AI Rewrite)' : 'AI Message Rewrite'}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                  <div>
                    <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                      {isRtl ? 'پیکربندی AI:' : 'AI Config:'}
                    </label>
                    <select
                      value={ruleFormData.ai_config_id || ''}
                      onChange={(e) => setRuleFormData({ ...ruleFormData, ai_config_id: e.target.value || undefined })}
                      className="input-field"
                      style={{ width: '100%' }}
                    >
                      <option value="">{isRtl ? '❌ غیرفعال (بدون بازنویسی)' : '❌ Disabled (no rewrite)'}</option>
                      {(Array.isArray(aiConfigs) ? aiConfigs : []).map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.is_enabled ? '🟢' : '🔴'} {a.name} [{a.provider}/{a.model}]
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                      {isRtl ? 'پرامپت بازنویسی:' : 'Rewrite Prompt:'}
                    </label>
                    <select
                      value={ruleMeta.prompt_id || ''}
                      onChange={(e) => setRuleMeta({ ...ruleMeta, prompt_id: e.target.value || undefined })}
                      className="input-field"
                      style={{ width: '100%' }}
                    >
                      <option value="">{isRtl ? 'پیش‌فرض (بدون پرامپت سیستمی)' : 'Default (no system prompt)'}</option>
                      {(Array.isArray(promptTemplates) ? promptTemplates : []).map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.is_system ? '⭐' : '👤'} {p.name} (v{p.current_version})
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                      {isRtl ? 'رفتار در خطای AI:' : 'On AI Failure:'}
                    </label>
                    <select
                      value={ruleFormData.ai_fallback_policy || 'DROP'}
                      onChange={(e) => setRuleFormData({ ...ruleFormData, ai_fallback_policy: e.target.value })}
                      className="input-field"
                      style={{ width: '100%' }}
                    >
                      <option value="SEND_ORIGINAL">{isRtl ? '🟢 ارسال متن اصلی (Fail Open)' : '🟢 Send original (Fail Open)'}</option>
                      <option value="DROP">{isRtl ? '🔴 عدم ارسال (Fail Closed)' : '🔴 Drop (Fail Closed)'}</option>
                      <option value="RETRY">{isRtl ? '🔁 تلاش مجدد (Retry)' : '🔁 Retry'}</option>
                      <option value="QUARANTINE">{isRtl ? '🔒 قرنطینه' : '🔒 Quarantine'}</option>
                    </select>
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                      {isRtl ? 'Timeout (ثانیه):' : 'Timeout (s):'}
                    </label>
                    <input
                      type="number"
                      min={1}
                      max={120}
                      value={ruleFormData.ai_timeout_seconds ?? 15}
                      onChange={(e) => setRuleFormData({ ...ruleFormData, ai_timeout_seconds: Number(e.target.value) || 15 })}
                      className="input-field"
                      style={{ width: '100%' }}
                    />
                  </div>
                </div>
                <div style={{ fontSize: '0.7rem', color: '#64748b', marginTop: 8 }}>
                  {isRtl
                    ? '💡 در حالت Fail Open، اگر AI در دسترس نباشد پیام اصلی بدون تغییر ارسال می‌شود و رویداد در لاگ ثبت می‌گردد.'
                    : '💡 Fail Open sends the original text if AI fails, and logs the fallback event.'}
                </div>
              </div>

              <div className="modal-sheet-actions" style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 24 }}>
                <button type="button" onClick={() => setIsRuleModalOpen(false)} className="btn btn-secondary">
                  {isRtl ? 'انصراف' : 'Cancel'}
                </button>
                {(() => {
                  const normEp = (s?: string) => (s || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
                  const s = normEp(ruleFormData.source_chat_id)
                  const t = normEp(ruleFormData.target_chat_id)
                  const im = normEp(ruleFormData.intermediate_channel_id)
                  const hasLoop = Boolean(s && t && s === t) ||
                    Boolean(ruleFormData.use_intermediate && im && (im === s || im === t))
                  return (
                    <button
                      type="submit"
                      disabled={hasLoop}
                      className="btn btn-primary"
                      style={{
                        opacity: hasLoop ? 0.45 : 1,
                        cursor: hasLoop ? 'not-allowed' : 'pointer',
                      }}
                    >
                      <Check size={16} />
                      <span>{isRtl ? 'ذخیره قانون' : 'Save Rule'}</span>
                    </button>
                  )
                })()}
              </div>
            </form>
            </div>
          </div>
        </div>
      )}

      {/* CREATE / EDIT AI MODAL */}
      {isAiModalOpen && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.8)',
            backdropFilter: 'blur(8px)',
            zIndex: 999,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 20,
          }}
        >
          <div
            className="glass-panel"
            style={{
              width: '100%',
              maxWidth: 580,
              padding: 28,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
              <h3 style={{ fontSize: '1.2rem', fontWeight: 800 }}>
                {isRtl ? 'تنظیمات موتور هوش مصنوعی' : 'AI Engine Configuration'}
              </h3>
              <button onClick={() => setIsAiModalOpen(false)} className="btn btn-secondary" style={{ padding: 6 }}>
                <X size={16} />
              </button>
            </div>

            <form onSubmit={handleSaveAI}>
              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {isRtl ? 'نام پیکربندی:' : 'Configuration Name:'} *
                </label>
                <input
                  type="text"
                  required
                  value={aiFormData.name || ''}
                  onChange={(e) => setAiFormData({ ...aiFormData, name: e.target.value })}
                  className="input-field"
                  placeholder="OpenAI GPT-4o Fast"
                />
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {isRtl ? 'ارائه‌دهنده (Provider):' : 'Provider:'}
                  </label>
                  <select
                    value={aiFormData.provider || 'OPENAI'}
                    onChange={(e) => setAiFormData({ ...aiFormData, provider: e.target.value })}
                    className="input-field"
                  >
                    <option value="OPENAI">OpenAI</option>
                    <option value="ANTHROPIC">Anthropic</option>
                    <option value="CUSTOM">Custom / Local LLM</option>
                  </select>
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {isRtl ? 'نام مدل:' : 'Model Name:'}
                  </label>
                  <input
                    type="text"
                    required
                    value={aiFormData.model || ''}
                    onChange={(e) => setAiFormData({ ...aiFormData, model: e.target.value })}
                    className="input-field"
                    placeholder="gpt-4o"
                  />
                </div>
              </div>

              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  API Key:
                </label>
                <input
                  type="password"
                  value={aiFormData.api_key || ''}
                  onChange={(e) => setAiFormData({ ...aiFormData, api_key: e.target.value })}
                  className="input-field"
                  placeholder="sk-..."
                />
              </div>

              <div style={{ marginBottom: 16 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  System Prompt:
                </label>
                <textarea
                  rows={3}
                  value={aiFormData.system_prompt || ''}
                  onChange={(e) => setAiFormData({ ...aiFormData, system_prompt: e.target.value })}
                  className="input-field"
                  placeholder="You are an expert forex signal translator..."
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
                <button type="button" onClick={() => setIsAiModalOpen(false)} className="btn btn-secondary">
                  {isRtl ? 'انصراف' : 'Cancel'}
                </button>
                <button type="submit" className="btn btn-primary">
                  <Check size={16} />
                  <span>{isRtl ? 'ذخیره' : 'Save'}</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* CREATE / EDIT FILTER MODAL */}
      {isFilterModalOpen && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.8)',
            backdropFilter: 'blur(8px)',
            zIndex: 999,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 20,
          }}
        >
          <div
            className="glass-panel"
            style={{
              width: '100%',
              maxWidth: 580,
              padding: 28,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
              <h3 style={{ fontSize: '1.2rem', fontWeight: 800 }}>
                {isRtl ? 'قاعده فیلتر محتوا' : 'Content Filter Rule'}
              </h3>
              <button onClick={() => setIsFilterModalOpen(false)} className="btn btn-secondary" style={{ padding: 6 }}>
                <X size={16} />
              </button>
            </div>

            <form onSubmit={handleSaveFilter}>
              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {isRtl ? 'نام فیلتر:' : 'Filter Name:'} *
                </label>
                <input
                  type="text"
                  required
                  value={filterFormData.name || ''}
                  onChange={(e) => setFilterFormData({ ...filterFormData, name: e.target.value })}
                  className="input-field"
                  placeholder="Anti-Spam Filter"
                />
              </div>

              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {isRtl ? 'کلمات مسدود (با ویرگول جدا کنید):' : 'Blacklist Keywords (comma-separated):'}
                </label>
                <input
                  type="text"
                  value={filterFormData.blacklist_keywords?.join(', ') || ''}
                  onChange={(e) =>
                    setFilterFormData({
                      ...filterFormData,
                      blacklist_keywords: e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
                    })
                  }
                  className="input-field"
                  placeholder="JOIN, SCAM, PROMO"
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 20 }}>
                <button type="button" onClick={() => setIsFilterModalOpen(false)} className="btn btn-secondary">
                  {isRtl ? 'انصراف' : 'Cancel'}
                </button>
                <button type="submit" className="btn btn-primary">
                  <Check size={16} />
                  <span>{isRtl ? 'ذخیره فیلتر' : 'Save Filter'}</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

function UserBar({ lang }: { lang: 'fa' | 'en' }) {
  const [user, setUser] = useState<{ username: string; is_admin: boolean } | null>(null)

  useEffect(() => {
    api.me().then(setUser).catch(() => {})
  }, [])

  const handleLogout = () => {
    api.logout()
    try {
      localStorage.removeItem('auth_token')
    } catch {}
    window.dispatchEvent(new Event('auth_required'))
  }

  if (!user) return null
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        padding: '8px 16px',
        marginBottom: 16,
        background: 'rgba(15, 23, 42, 0.6)',
        borderRadius: 12,
        border: '1px solid rgba(255, 255, 255, 0.08)',
        fontSize: '0.85rem',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#10b981', boxShadow: '0 0 8px #10b981' }} />
        <span style={{ color: '#e2e8f0', fontWeight: 600 }}>
          {user.is_admin ? '👑 ' : '👤 '}
          {user.username}
        </span>
        {user.is_admin && (
          <span style={{ fontSize: '0.7rem', padding: '2px 6px', borderRadius: 4, background: 'rgba(99, 102, 241, 0.2)', color: '#818cf8' }}>
            ADMIN
          </span>
        )}
      </div>
      <button
        onClick={handleLogout}
        style={{
          padding: '4px 12px',
          borderRadius: 8,
          border: '1px solid rgba(239, 68, 68, 0.3)',
          background: 'rgba(239, 68, 68, 0.1)',
          cursor: 'pointer',
          fontSize: '0.8rem',
          color: '#f87171',
          fontWeight: 600,
        }}
      >
        {lang === 'fa' ? 'خروج از حساب' : 'Logout'}
      </button>
    </div>
  )
}
export default App

// ===========================================================================
// AI Rewrite Prompt Editor & Tester (shared bot + dashboard logic via API)
// ===========================================================================
function PromptEditorPanel({ prompts, aiConfigs, onSaved, isRtl }: {
  prompts: PromptTemplate[]
  aiConfigs: AIConfig[]
  onSaved: () => Promise<void>
  isRtl: boolean
}) {
  const [selected, setSelected] = useState<PromptTemplate | null>(null)
  const [form, setForm] = useState({ name: '', description: '', system_prompt: '', user_prompt_template: '{text}', target_language: 'auto' })
  const [testText, setTestText] = useState('💎 XAUUSD SIGNAL: BUY NOW @ 2655 | TP 2680 | SL 2640 https://t.me/signal #gold')
  const [testResult, setTestResult] = useState<TestPromptResponse | null>(null)
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)

  const openEditor = (p: PromptTemplate | null) => {
    setSelected(p)
    setTestResult(null)
    setForm(p ? {
      name: p.name, description: p.description || '',
      system_prompt: p.system_prompt,
      user_prompt_template: p.user_prompt_template || '{text}',
      target_language: p.target_language || 'auto',
    } : { name: '', description: '', system_prompt: '', user_prompt_template: '{text}', target_language: 'auto' })
  }

  const handleSave = async () => {
    if (!form.system_prompt.trim()) return
    setSaving(true)
    try {
      await api.savePromptTemplate({ ...form, id: selected?.id, current_version: selected?.current_version } as any)
      showToast(isRtl ? 'پرامپت ذخیره شد' : 'Prompt saved')
      await onSaved()
      openEditor(null)
    } catch (e: any) {
      showToast(e.message || 'Error saving prompt')
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await api.deletePromptTemplate(id)
      showToast(isRtl ? 'پرامپت حذف شد' : 'Prompt deleted')
      await onSaved()
      if (selected?.id === id) openEditor(null)
    } catch (e: any) {
      showToast(e.message || 'Cannot delete system prompt')
    }
  }

  const handleTest = async () => {
    if (!selected || !aiConfigs.length) return
    setTesting(true)
    setTestResult(null)
    try {
      const r = await api.testPrompt({
        prompt_id: selected.id,
        ai_config_id: aiConfigs[0].id,
        sample_text: testText,
        version: 0,
      })
      setTestResult(r)
    } catch (e: any) {
      showToast(e.message || 'Test failed')
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="glass-panel" style={{ padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <h2 style={{ fontSize: '1.25rem', fontWeight: 800, display: 'flex', alignItems: 'center', gap: 10 }}>
            ✍️ {isRtl ? 'پرامپت‌های بازنویسی (AI Rewrite Prompts)' : 'Rewrite Prompt Editor'}
          </h2>
          <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
            {isRtl
              ? 'پرامپت‌های قابل استفاده در قوانین فوروارد. پرامپت‌های ⭐ سیستمی هستند و فقط قابل مشاهده.'
              : 'Reusable rewrite prompts for forward rules. ⭐ = system prompts (read-only).'}
          </p>
        </div>
        <button onClick={() => openEditor(null)} className="btn btn-primary" style={{ padding: '8px 14px' }}>
          + {isRtl ? 'پرامپت جدید' : 'New Prompt'}
        </button>
      </div>

      {/* Prompt list */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 12, marginBottom: 20 }}>
        {(Array.isArray(prompts) ? prompts : []).map((p) => (
          <div key={p.id} style={{
            background: 'rgba(15, 23, 42, 0.6)', border: selected?.id === p.id ? '1px solid rgba(168,85,247,0.6)' : '1px solid rgba(255,255,255,0.06)',
            borderRadius: 10, padding: 14, cursor: 'pointer',
          }} onClick={() => openEditor(p)}>
            <div style={{ fontWeight: 700, fontSize: '0.85rem', marginBottom: 4 }}>
              {p.is_system ? '⭐' : '👤'} {p.name} <span style={{ color: '#64748b', fontWeight: 400 }}>v{p.current_version}</span>
            </div>
            <div style={{ fontSize: '0.72rem', color: '#94a3b8', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
              {p.description || p.system_prompt.slice(0, 100)}
            </div>
            {!p.is_system && (
              <button
                onClick={(e) => { e.stopPropagation(); handleDelete(p.id) }}
                className="btn btn-danger icon-btn" style={{ padding: 4, marginTop: 8, float: 'right' }}
                title={isRtl ? 'حذف' : 'Delete'}
              >
                🗑
              </button>
            )}
          </div>
        ))}
      </div>

      {/* Editor form */}
      {form && (selected === null || selected !== null) && form.system_prompt !== undefined && (
        <div style={{ borderTop: '1px solid rgba(255,255,255,0.06)', paddingTop: 18 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
            <div>
              <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                {isRtl ? 'نام:' : 'Name:'} *
              </label>
              <input className="input-field" style={{ width: '100%' }} value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={isRtl ? 'مثلاً: بازنویسی رسمی' : 'e.g. Formal Rewrite'} />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                {isRtl ? 'زبان خروجی:' : 'Output Language:'}
              </label>
              <select className="input-field" style={{ width: '100%' }} value={form.target_language}
                onChange={(e) => setForm({ ...form, target_language: e.target.value })}>
                <option value="auto">Auto</option>
                <option value="fa">فارسی</option>
                <option value="en">English</option>
              </select>
            </div>
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
              {isRtl ? 'توضیحات:' : 'Description:'}
            </label>
            <input className="input-field" style={{ width: '100%' }} value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
              {isRtl ? 'System Prompt (دستور اصلی به AI):' : 'System Prompt:'} *
              {selected?.is_system && <span style={{ color: '#f59e0b' }}> — {isRtl ? 'سیستمی، فقط‌خواندنی' : 'system, read-only'}</span>}
            </label>
            <textarea
              className="input-field"
              style={{ width: '100%', minHeight: 110, resize: 'vertical', fontFamily: 'monospace', fontSize: '0.78rem' }}
              value={form.system_prompt}
              readOnly={!!selected?.is_system}
              onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
              placeholder={isRtl
                ? 'متن را با حفظ اعداد، لینک‌ها و نام‌ها بازنویسی کن. اطلاعات جدید اختراع نکن. فقط متن بازنویسی‌شده را برگردان.'
                : 'Rewrite the text preserving numbers, links, and names. Never invent facts. Return only the rewritten text.'}
            />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
              {isRtl ? 'قالب پیام کاربر (User Template):' : 'User Prompt Template:'}
            </label>
            <input className="input-field" style={{ width: '100%', fontFamily: 'monospace', fontSize: '0.78rem' }}
              value={form.user_prompt_template} readOnly={!!selected?.is_system}
              onChange={(e) => setForm({ ...form, user_prompt_template: e.target.value })} placeholder="{text}" />
            <div style={{ fontSize: '0.7rem', color: '#64748b', marginTop: 4 }}>
              {isRtl ? 'متغیر {text} جای متن پیام قرار می‌گیرد.' : '{text} is replaced with the message body.'}
            </div>
          </div>

          {/* Test panel */}
          <div style={{ background: 'rgba(15, 23, 42, 0.6)', borderRadius: 10, padding: 14, border: '1px solid rgba(255,255,255,0.06)' }}>
            <div style={{ fontSize: '0.78rem', fontWeight: 700, color: '#a5b4fc', marginBottom: 8 }}>
              🧪 {isRtl ? 'تست پرامپت (بدون ارسال پیام)' : 'Prompt Test (no message sent)'}
            </div>
            <textarea className="input-field" style={{ width: '100%', minHeight: 60, marginBottom: 8, fontSize: '0.78rem' }}
              value={testText} onChange={(e) => setTestText(e.target.value)} />
            <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
              <button
                onClick={handleTest}
                disabled={testing || !selected || !aiConfigs.length}
                className="btn btn-secondary" style={{ padding: '6px 12px' }}
              >
                {testing ? '⏳' : '▶'} {isRtl ? 'تست با AI' : 'Run AI Test'}
              </button>
              {!aiConfigs.length && <span style={{ fontSize: '0.72rem', color: '#f87171' }}>
                {isRtl ? 'اول یک پیکربندی AI بسازید' : 'Create an AI config first'}
              </span>}
            </div>
            {testResult && (
              <div style={{ marginTop: 10 }}>
                {testResult.success ? (
                  <div>
                    <div style={{ fontSize: '0.72rem', color: '#64748b', marginBottom: 4 }}>
                      ✅ {testResult.model_used} | {testResult.execution_duration_ms.toFixed(0)}ms | v{testResult.prompt_version_used}
                    </div>
                    <div style={{ background: 'rgba(34,197,94,0.08)', border: '1px solid rgba(34,197,94,0.3)', borderRadius: 8, padding: 10, fontSize: '0.8rem', whiteSpace: 'pre-wrap' }}>
                      {testResult.transformed_text}
                    </div>
                  </div>
                ) : (
                  <div style={{ color: '#f87171', fontSize: '0.78rem' }}>
                    ❌ {testResult.error_code}: {testResult.error_message}
                  </div>
                )}
              </div>
            )}
          </div>

          {!selected?.is_system && form.system_prompt && (
            <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
              <button onClick={handleSave} disabled={saving || !form.name.trim()} className="btn btn-primary" style={{ padding: '8px 16px' }}>
                💾 {saving ? (isRtl ? 'در حال ذخیره…' : 'Saving…') : (isRtl ? 'ذخیره پرامپت' : 'Save Prompt')}
              </button>
              {selected && (
                <button onClick={() => openEditor(null)} className="btn btn-secondary" style={{ padding: '8px 16px' }}>
                  {isRtl ? 'انصراف' : 'Cancel'}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
