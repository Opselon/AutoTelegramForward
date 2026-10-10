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
} from 'lucide-react'
import { api } from './api'
import type {
  ForwardRule,
  SaveRuleRequest,
  Session,
  AIConfig,
  SaveAIConfigRequest,
  FilterRule,
  SaveFilterRuleRequest,
  DeliveryJob,
  DeadLetterJob,
  DeliveryStats,
  RuleLiveStat,
  RecentError,
  GatewaySystemInfo,
  SimulateResponse,
  StatsResponse,
  LogItem,
  LogStats,
} from './types'

export function App() {
  const [lang, setLang] = useState<'fa' | 'en'>('fa')
  const [activeTab, setActiveTab] = useState<'rules' | 'simulator' | 'ai' | 'filters' | 'sessions' | 'queue' | 'devops'>('rules')
  const [rules, setRules] = useState<ForwardRule[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [aiConfigs, setAiConfigs] = useState<AIConfig[]>([])
  const [filterRules, setFilterRules] = useState<FilterRule[]>([])
  const [queueJobs, setQueueJobs] = useState<DeliveryJob[]>([])
  const [dlqJobs, setDlqJobs] = useState<DeadLetterJob[]>([])
  const [deliveryStats, setDeliveryStats] = useState<DeliveryStats | null>(null)
  const [ruleLiveStats, setRuleLiveStats] = useState<RuleLiveStat[]>([])
  const [recentErrors, setRecentErrors] = useState<RecentError[]>([])
  const [errorBanner, setErrorBanner] = useState<string | null>(null)
  const [gatewayInfo, setGatewayInfo] = useState<GatewaySystemInfo | null>(null)
  const [stats, setStats] = useState<StatsResponse | null>(null)
  const [logs, setLogs] = useState<LogItem[]>([])
  const [logFilterService, setLogFilterService] = useState<string>('all')
  const [logFilterLevel, setLogFilterLevel] = useState<string>('all')
  const [logSearch, setLogSearch] = useState<string>('')
  const [logAutoRefresh, setLogAutoRefresh] = useState<boolean>(true)
  const [logStats, setLogStats] = useState<LogStats | null>(null)
  const [copiedLogId, setCopiedLogId] = useState<string | number | null>(null)
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
  const [simuring, setSimulating] = useState(false)

  const isRtl = lang === 'fa'

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 4000)
  }

  const loadData = async () => {
    setLoading(true)
    try {
      const [r, s, ai, f, q, dlq, gw, st, l, ds] = await Promise.all([
        api.getRules().catch(() => []),
        api.getSessions().catch(() => []),
        api.getAIConfigs().catch(() => []),
        api.getFilters().catch(() => []),
        api.getQueueJobs().catch(() => []),
        api.getDLQJobs().catch(() => []),
        api.getGatewayInfo().catch(() => null),
        api.getStats().catch(() => null),
        api.getLogs().catch(() => []),
        api.getDeliveryStats().catch(() => null),
      ])
      setRules(Array.isArray(r) ? r : [])
      setSessions(Array.isArray(s) ? s : [])
      setAiConfigs(Array.isArray(ai) ? ai : [])
      setFilterRules(Array.isArray(f) ? f : [])
      setQueueJobs(Array.isArray(q) ? q : [])
      setDlqJobs(Array.isArray(dlq) ? dlq : [])
      setGatewayInfo(gw)
      setStats(st)
      setLogs(Array.isArray(l) ? l : [])
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
          // Python logs often arrive as "ValueError: ValueError <msg>" — strip the
          // duplicated exception class so the banner reads as one clean sentence.
          if (name && detail.toLowerCase().startsWith(name.toLowerCase())) {
            detail = detail.slice(name.length).replace(/^[\s:：]+/, '')
          }
          const text = (name && detail) ? `${name}: ${detail}` : (detail || name)
          return text.slice(0, 220) || null
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

  useEffect(() => {
    loadData()
    const timer = setInterval(() => {
      api.getStats().then(setStats).catch(() => {})
      api.getGatewayInfo().then(setGatewayInfo).catch(() => {})
    }, 10000)
    return () => clearInterval(timer)
  }, [])

  // Proactive live log stream polling when on devops tab and auto-refresh enabled
  useEffect(() => {
    if (activeTab !== 'devops') return
    fetchLogs(logFilterService, logFilterLevel, logSearch)
    if (!logAutoRefresh) return
    const logTimer = setInterval(() => {
      fetchLogs(logFilterService, logFilterLevel, logSearch)
    }, 3000)
    return () => clearInterval(logTimer)
  }, [activeTab, logAutoRefresh, logFilterService, logFilterLevel, logSearch])

  // Rules Handlers
  const handleToggleRule = async (id: string) => {
    try {
      const updated = await api.toggleRule(id)
      setRules((prev) => prev.map((r) => (r.id === id ? updated : r)))
      showToast(isRtl ? 'وضعیت قانون با موفقیت تغییر کرد' : 'Rule status updated')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleDeleteRule = async (id: string) => {
    if (!confirm(isRtl ? 'آیا از حذف این قانون اطمینان دارید؟' : 'Are you sure you want to delete this rule?')) return
    try {
      await api.deleteRule(id)
      setRules((prev) => prev.filter((r) => r.id !== id))
      showToast(isRtl ? 'قانون با موفقیت حذف شد' : 'Rule deleted successfully')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleQuickRouteSwitch = async (ruleId: string, pathType: 'route1_direct' | 'route2_vip_hop' | 'route3_native') => {
    try {
      const updated = await api.quickSetRoutePath(ruleId, {
        path_type: pathType,
      })
      setRules((prev) => prev.map((r) => (r.id === ruleId ? updated : r)))
      showToast(isRtl ? 'مسیر قانون با موفقیت تغییر یافت' : 'Rule route switched successfully')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleSaveRule = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      if (!ruleFormData.source_chat_id || !ruleFormData.target_chat_id) {
        showToast(isRtl ? 'لطفا شناسه‌های مبدا و مقصد را وارد کنید' : 'Source and target IDs are required')
        return
      }
      const saved = await api.saveRule({
        ...ruleFormData,
        custom_metadata_json: JSON.stringify(ruleMeta),
      } as SaveRuleRequest)
      setRules((prev) => {
        const idx = prev.findIndex((r) => r.id === saved.id)
        if (idx >= 0) {
          const next = [...prev]
          next[idx] = saved
          return next
        }
        return [saved, ...prev]
      })
      setIsRuleModalOpen(false)
      showToast(isRtl ? 'قانون با موفقیت ذخیره شد' : 'Rule saved successfully')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  // AI Handlers
  const handleSaveAI = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      if (!aiFormData.name || !aiFormData.provider || !aiFormData.model) {
        showToast(isRtl ? 'نام، ارائه‌دهنده و مدل الزامی هستند' : 'Name, provider, and model are required')
        return
      }
      const saved = await api.saveAIConfig(aiFormData as SaveAIConfigRequest)
      setAiConfigs((prev) => {
        const idx = prev.findIndex((a) => a.id === saved.id)
        if (idx >= 0) {
          const next = [...prev]
          next[idx] = saved
          return next
        }
        return [saved, ...prev]
      })
      setIsAiModalOpen(false)
      showToast(isRtl ? 'پیکربندی هوش مصنوعی ذخیره شد' : 'AI Config saved successfully')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleDeleteAI = async (id: string) => {
    if (!confirm(isRtl ? 'آیا از حذف این پیکربندی هوش مصنوعی مطمئن هستید؟' : 'Delete this AI Config?')) return
    try {
      await api.deleteAIConfig(id)
      setAiConfigs((prev) => prev.filter((a) => a.id !== id))
      showToast(isRtl ? 'تنظیمات هوش مصنوعی حذف شد' : 'AI Config deleted')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  // Filter Handlers
  const handleSaveFilter = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      if (!filterFormData.name) {
        showToast(isRtl ? 'نام فیلتر الزامی است' : 'Filter name is required')
        return
      }
      const saved = await api.saveFilter(filterFormData as SaveFilterRuleRequest)
      setFilterRules((prev) => {
        const idx = prev.findIndex((f) => f.id === saved.id)
        if (idx >= 0) {
          const next = [...prev]
          next[idx] = saved
          return next
        }
        return [saved, ...prev]
      })
      setIsFilterModalOpen(false)
      showToast(isRtl ? 'قاعده فیلتر ذخیره شد' : 'Filter rule saved successfully')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleDeleteFilter = async (id: string) => {
    if (!confirm(isRtl ? 'آیا از حذف این فیلتر مطمئن هستید؟' : 'Delete this filter rule?')) return
    try {
      await api.deleteFilter(id)
      setFilterRules((prev) => prev.filter((f) => f.id !== id))
      showToast(isRtl ? 'قاعده فیلتر حذف شد' : 'Filter rule deleted')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  // DLQ Handlers
  const handleRetryDLQ = async (id: string) => {
    try {
      await api.retryDLQ(id)
      setDlqJobs((prev) => prev.filter((j) => j.id !== id))
      showToast(isRtl ? 'پیام برای تلاش مجدد به صف بازگردانده شد' : 'Job requeued for retry')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handlePurgeDLQ = async () => {
    if (!confirm(isRtl ? 'آیا از پاک‌سازی کامل صف خطاهای ناموفق اطمینان دارید؟' : 'Purge all DLQ entries?')) return
    try {
      await api.purgeDLQ()
      setDlqJobs([])
      showToast(isRtl ? 'صف خطاهای DLQ تخلیه شد' : 'DLQ purged successfully')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  // Rule pause / resume (mirrors bot's pause/resume feature)
  const handlePauseRule = async (id: string) => {
    try {
      await api.pauseRule(id)
      setRules((prev) => prev.map((r) => (r.id === id ? { ...r, is_paused: true } : r)))
      showToast(isRtl ? 'قانون متوقف شد' : 'Rule paused')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleResumeRule = async (id: string) => {
    try {
      await api.resumeRule(id)
      setRules((prev) => prev.map((r) => (r.id === id ? { ...r, is_paused: false } : r)))
      showToast(isRtl ? 'قانون از سر گرفته شد' : 'Rule resumed')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  // Session backup / terminate (mirrors bot's backup + session control)
  const handleBackupSession = async (sess: Session) => {
    try {
      const res = await api.backupSession(sess.id)
      if (res.success && res.encrypted_session_data) {
        const blob = new Blob([res.encrypted_session_data], { type: 'application/octet-stream' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `atf-session-${sess.phone_number || sess.user_id || sess.id}.bin`
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        URL.revokeObjectURL(url)
        showToast(isRtl ? 'بکاپ سشن دانلود شد' : 'Session backup downloaded')
      } else {
        showToast(res.message || (isRtl ? 'بکاپ ناموفق بود' : 'Backup failed'))
      }
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleTerminateSession = async (sess: Session) => {
    if (!confirm(isRtl ? `آیا از قطع سشن ${sess.first_name || sess.username || sess.id} مطمئن هستید؟` : `Terminate session ${sess.first_name || sess.username || sess.id}?`)) return
    try {
      const res = await api.terminateSession(sess.id)
      if (res.success) {
        setSessions((prev) => prev.filter((s) => s.id !== sess.id))
        showToast(isRtl ? 'سشن قطع شد' : 'Session terminated')
      } else {
        showToast(res.message || (isRtl ? 'قطع ناموفق بود' : 'Terminate failed'))
      }
    } catch (e: any) {
      showToast(e.message)
    }
  }

  // Simulator
  const handleRunSimulation = async () => {
    setSimulating(true)
    try {
      const res = await api.simulate({
        rule_id: simSelectedRuleId || undefined,
        test_text: simText,
        forward_origin_chat_id: simOriginChatId || undefined,
        forward_origin_title: simOriginTitle || undefined,
        forward_origin_username: simOriginUser || undefined,
        has_media: simHasMedia,
        is_protected: simIsProtected,
      })
      setSimResult(res)
    } catch (e: any) {
      showToast(e.message || 'Simulation error')
    } finally {
      setSimulating(false)
    }
  }

  return (
    <div dir={isRtl ? 'rtl' : 'ltr'} style={{ padding: '24px 20px', minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* User bar */}
      <UserBar lang={lang} />
      {/* Proactive Error Banner — newest ERROR/WARN from the delivery pipeline */}
      {errorBanner && (
        <div
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            gap: 10,
            padding: '10px 14px',
            marginBottom: 16,
            borderRadius: 10,
            background: 'rgba(239, 68, 68, 0.10)',
            border: '1px solid rgba(239, 68, 68, 0.35)',
            fontSize: '0.82rem',
            color: '#fca5a5',
          }}
        >
          <span style={{ flexShrink: 0, lineHeight: 1.4 }}>⚠️</span>
          <span dir="ltr" style={{ flex: 1, textAlign: 'left', wordBreak: 'break-word', fontFamily: "'JetBrains Mono', Consolas, monospace", fontSize: '0.76rem' }}>
            {errorBanner}
          </span>
          <button
            onClick={() => setErrorBanner(null)}
            style={{ background: 'none', border: 'none', color: '#f87171', cursor: 'pointer', padding: 2, flexShrink: 0 }}
            title={isRtl ? 'بستن' : 'Dismiss'}
          >
            <X size={14} />
          </button>
        </div>
      )}
      {/* Toast Notification */}
      {toast && (
        <div
          style={{
            position: 'fixed',
            bottom: 24,
            right: isRtl ? 24 : 'auto',
            left: isRtl ? 'auto' : 24,
            background: 'rgba(30, 41, 59, 0.95)',
            border: '1px solid rgba(99, 102, 241, 0.5)',
            boxShadow: '0 10px 25px -5px rgba(0, 0, 0, 0.5)',
            color: '#fff',
            padding: '12px 20px',
            borderRadius: 12,
            zIndex: 9999,
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            fontSize: '0.9rem',
          }}
        >
          <Sparkles size={18} color="#818cf8" />
          <span>{toast}</span>
        </div>
      )}

      {/* Header Bar */}
      <header
        className="glass-panel"
        style={{
          padding: '16px 24px',
          marginBottom: 24,
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 16,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: 12,
              background: 'linear-gradient(135deg, #6366f1 0%, #a855f7 100%)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              boxShadow: '0 4px 15px rgba(99, 102, 241, 0.4)',
            }}
          >
            <Radio size={24} color="#fff" />
          </div>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <h1 style={{ fontSize: '1.25rem', fontWeight: 800, letterSpacing: '-0.02em' }}>
                AutoTelegramForward <span style={{ color: '#818cf8', fontWeight: 600 }}>PRO GATEWAY</span>
              </h1>
              <span className="badge badge-vip" style={{ fontSize: '0.75rem', padding: '2px 8px' }}>
                v{gatewayInfo?.version ?? '1.2.2'} RELEASE
              </span>
            </div>
            <p style={{ fontSize: '0.8rem', color: '#94a3b8', marginTop: 2 }}>
              {isRtl
                ? 'سامانه یکپارچه فوروارد هوشمند، میکروسرویس Rust + React و درگاه DevOps'
                : 'Unified Smart Telegram Forwarding Gateway & Microservice Platform'}
            </p>
          </div>
        </div>

        {/* Microservice Health Indicators */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              background: 'rgba(15, 23, 42, 0.6)',
              padding: '6px 12px',
              borderRadius: 8,
              border: '1px solid rgba(255, 255, 255, 0.06)',
              fontSize: '0.8rem',
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: stats?.atf_core_online ? '#10b981' : '#ef4444',
                boxShadow: stats?.atf_core_online ? '0 0 8px #10b981' : '0 0 8px #ef4444',
              }}
            />
            <span style={{ color: '#cbd5e1' }}>Core gRPC:6001</span>
          </div>

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              background: 'rgba(15, 23, 42, 0.6)',
              padding: '6px 12px',
              borderRadius: 8,
              border: '1px solid rgba(255, 255, 255, 0.06)',
              fontSize: '0.8rem',
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: stats?.atf_logger_online ? '#10b981' : '#ef4444',
                boxShadow: stats?.atf_logger_online ? '0 0 8px #10b981' : '0 0 8px #ef4444',
              }}
            />
            <span style={{ color: '#cbd5e1' }}>Logger:6002</span>
          </div>

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              background: 'rgba(15, 23, 42, 0.6)',
              padding: '6px 12px',
              borderRadius: 8,
              border: '1px solid rgba(255, 255, 255, 0.06)',
              fontSize: '0.8rem',
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: '#10b981',
                boxShadow: '0 0 8px #10b981',
              }}
            />
            <span style={{ color: '#cbd5e1' }}>Rust Web:8088</span>
          </div>

          {/* Language Toggle */}
          <button
            onClick={() => setLang(lang === 'fa' ? 'en' : 'fa')}
            className="btn btn-secondary"
            style={{ padding: '6px 12px', fontSize: '0.8rem' }}
          >
            <Globe size={14} />
            <span>{lang === 'fa' ? 'English' : 'فارسی'}</span>
          </button>

          {/* Reload Button */}
          <button
            onClick={loadData}
            disabled={loading}
            className="btn btn-secondary"
            style={{ padding: '6px 12px', fontSize: '0.8rem' }}
            title={isRtl ? 'به‌روزرسانی داده‌ها' : 'Refresh'}
          >
            <RotateCcw size={14} />
          </button>
        </div>
      </header>

      {/* Overview Stat Badges */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
          gap: 16,
          marginBottom: 24,
        }}
      >
        <div className="glass-panel" style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{ padding: 10, borderRadius: 10, background: 'rgba(99, 102, 241, 0.15)', color: '#818cf8' }}>
            <Layers size={22} />
          </div>
          <div>
            <div style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{isRtl ? 'کل قوانین فعال' : 'Active Rules'}</div>
            <div style={{ fontSize: '1.4rem', fontWeight: 800 }}>
              {stats?.active_rules ?? 0} <span style={{ fontSize: '0.85rem', color: '#64748b' }}>/ {stats?.total_rules ?? 0}</span>
            </div>
          </div>
        </div>

        <div className="glass-panel" style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{ padding: 10, borderRadius: 10, background: 'rgba(245, 158, 11, 0.15)', color: '#fbbf24' }}>
            <Send size={22} />
          </div>
          <div>
            <div style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{isRtl ? 'فوروارد ۲۴ ساعت' : 'Forwarded 24h'}</div>
            <div style={{ fontSize: '1.4rem', fontWeight: 800, color: '#fbbf24' }}>
              {stats?.total_forwarded_24h ?? 0}
            </div>
          </div>
        </div>

        <div className="glass-panel" style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{ padding: 10, borderRadius: 10, background: 'rgba(16, 185, 129, 0.15)', color: '#34d399' }}>
            <Users size={22} />
          </div>
          <div>
            <div style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{isRtl ? 'سشن‌های متصل' : 'Connected Sessions'}</div>
            <div style={{ fontSize: '1.4rem', fontWeight: 800 }}>
              {stats?.active_sessions ?? 0} <span style={{ fontSize: '0.85rem', color: '#64748b' }}>/ {stats?.total_sessions ?? 0}</span>
            </div>
          </div>
        </div>

        <div className="glass-panel" style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{ padding: 10, borderRadius: 10, background: 'rgba(239, 68, 68, 0.15)', color: '#f87171' }}>
            <Activity size={22} />
          </div>
          <div>
            <div style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{isRtl ? 'صف خطاها (DLQ)' : 'Dead Letter Queue'}</div>
            <div style={{ fontSize: '1.4rem', fontWeight: 800, color: dlqJobs.length > 0 ? '#ef4444' : '#94a3b8' }}>
              {dlqJobs.length}
            </div>
          </div>
        </div>
      </div>

      {/* Tabs Navigation */}
      <div
        className="tab-strip"
        style={{
          display: 'flex',
          gap: 8,
          borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
          paddingBottom: 12,
          marginBottom: 24,
          overflowX: 'auto',
        }}
      >
        <button
          onClick={() => setActiveTab('rules')}
          className="btn"
          style={{
            background: activeTab === 'rules' ? 'rgba(99, 102, 241, 0.2)' : 'transparent',
            color: activeTab === 'rules' ? '#818cf8' : '#94a3b8',
            border: activeTab === 'rules' ? '1px solid rgba(99, 102, 241, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'rules' ? 700 : 500,
          }}
        >
          <Radio size={16} />
          <span>{isRtl ? 'قوانین و مسیرها' : 'Rules & Routes'}</span>
          <span style={{ fontSize: '0.75rem', opacity: 0.7 }}>({rules.length})</span>
        </button>

        <button
          onClick={() => setActiveTab('simulator')}
          className="btn"
          style={{
            background: activeTab === 'simulator' ? 'rgba(245, 158, 11, 0.2)' : 'transparent',
            color: activeTab === 'simulator' ? '#fbbf24' : '#94a3b8',
            border: activeTab === 'simulator' ? '1px solid rgba(245, 158, 11, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'simulator' ? 700 : 500,
          }}
        >
          <PlayCircle size={16} />
          <span>{isRtl ? 'شبیه‌ساز زنده مسیر' : 'Smart Simulator'}</span>
          <span className="badge badge-vip" style={{ fontSize: '0.65rem', padding: '1px 6px' }}>TEST</span>
        </button>

        <button
          onClick={() => setActiveTab('ai')}
          className="btn"
          style={{
            background: activeTab === 'ai' ? 'rgba(168, 85, 247, 0.2)' : 'transparent',
            color: activeTab === 'ai' ? '#c084fc' : '#94a3b8',
            border: activeTab === 'ai' ? '1px solid rgba(168, 85, 247, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'ai' ? 700 : 500,
          }}
        >
          <Bot size={16} />
          <span>{isRtl ? 'هوش مصنوعی' : 'AI Intelligence'}</span>
          <span style={{ fontSize: '0.75rem', opacity: 0.7 }}>({aiConfigs.length})</span>
        </button>

        <button
          onClick={() => setActiveTab('filters')}
          className="btn"
          style={{
            background: activeTab === 'filters' ? 'rgba(59, 130, 246, 0.2)' : 'transparent',
            color: activeTab === 'filters' ? '#60a5fa' : '#94a3b8',
            border: activeTab === 'filters' ? '1px solid rgba(59, 130, 246, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'filters' ? 700 : 500,
          }}
        >
          <Filter size={16} />
          <span>{isRtl ? 'فیلترهای پیشرفته' : 'Advanced Filters'}</span>
          <span style={{ fontSize: '0.75rem', opacity: 0.7 }}>({filterRules.length})</span>
        </button>

        <button
          onClick={() => setActiveTab('sessions')}
          className="btn"
          style={{
            background: activeTab === 'sessions' ? 'rgba(16, 185, 129, 0.2)' : 'transparent',
            color: activeTab === 'sessions' ? '#34d399' : '#94a3b8',
            border: activeTab === 'sessions' ? '1px solid rgba(16, 185, 129, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'sessions' ? 700 : 500,
          }}
        >
          <Users size={16} />
          <span>{isRtl ? 'سشن‌های تلگرام' : 'Telegram Sessions'}</span>
          <span style={{ fontSize: '0.75rem', opacity: 0.7 }}>({sessions.length})</span>
        </button>

        <button
          onClick={() => setActiveTab('queue')}
          className="btn"
          style={{
            background: activeTab === 'queue' ? 'rgba(239, 68, 68, 0.2)' : 'transparent',
            color: activeTab === 'queue' ? '#f87171' : '#94a3b8',
            border: activeTab === 'queue' ? '1px solid rgba(239, 68, 68, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'queue' ? 700 : 500,
          }}
        >
          <Inbox size={16} />
          <span>{isRtl ? 'صف تحویل و DLQ' : 'Queue & DLQ'}</span>
          {dlqJobs.length > 0 && (
            <span style={{ background: '#ef4444', color: '#fff', borderRadius: '50%', padding: '1px 6px', fontSize: '0.7rem' }}>
              {dlqJobs.length}
            </span>
          )}
        </button>

        <button
          onClick={() => setActiveTab('devops')}
          className="btn"
          style={{
            background: activeTab === 'devops' ? 'rgba(148, 163, 184, 0.2)' : 'transparent',
            color: activeTab === 'devops' ? '#f1f5f9' : '#94a3b8',
            border: activeTab === 'devops' ? '1px solid rgba(255, 255, 255, 0.3)' : '1px solid transparent',
            fontWeight: activeTab === 'devops' ? 700 : 500,
          }}
        >
          <Server size={16} />
          <span>{isRtl ? 'درگاه DevOps و لاگ‌ها' : 'DevOps Gateway'}</span>
        </button>
      </div>

      {/* TAB 1: RULES & ROUTES */}
      {activeTab === 'rules' && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
            <div>
              <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>
                {isRtl ? 'قوانین فوروارد و مسیریابی هوشمند' : 'Smart Forwarding & Routing Rules'}
              </h2>
              <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                {isRtl
                  ? 'تعریف مسیرهای سه‌گانه، سیاست‌های بازنویسی، کنترل حفاظت تلگرام و سوئیچ سریع مسیر'
                  : 'Configure triple-routes, branding hops, bypass policies, and channel mappings'}
              </p>
            </div>
            <button
              onClick={() => {
                setRuleFormData({
                  routing_type: 'CHANNEL_TO_CHANNEL',
                  forward_mode: 'CUSTOM_HEADER_COPY',
                  message_category: 'VIP_ONLY',
                  use_intermediate: true,
                  intermediate_channel_id: '-1002222222222',
                  intermediate_channel_name: 'Brand Channel C',
                  fallback_mode: 'COPY_MESSAGE',
                  fallback_enabled: true,
                  is_active: true,
                  priority: 10,
                  link_policy: 'PRESERVE_ALL',
                  custom_header: '💎 VIP FOREX SIGNALS',
                  split_long_caption: true,
                  detection_criteria: {
                    match_mode: 'ANY',
                    text_contains: ['VIP', 'SIGNAL', 'BUY', 'SELL'],
                    forward_origin_chat_ids: ['-1001111111111'],
                  },
                })
                setRuleMeta({})
                setIsRuleModalOpen(true)
              }}
              className="btn btn-primary"
            >
              <Plus size={16} />
              <span>{isRtl ? 'افزودن قانون جدید' : 'New Rule'}</span>
            </button>
          </div>

          <div className="rules-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(420px, 1fr))', gap: 20 }}>
            {(Array.isArray(rules) ? rules : []).map((rule) => {
              const isVipHop = rule.use_intermediate && rule.message_category === 'VIP_ONLY'
              const isDirectCopy = !rule.use_intermediate && rule.forward_mode !== 'DIRECT_FORWARD'
              const isNative = !rule.use_intermediate && rule.forward_mode === 'DIRECT_FORWARD'
              const liveStat = (ruleLiveStats || []).find((st) => st.rule_id === rule.id)

              return (
                <div
                  key={rule.id}
                  className="glass-panel"
                  style={{
                    padding: '20px',
                    position: 'relative',
                    border: isVipHop ? '1px solid rgba(245, 158, 11, 0.4)' : '1px solid var(--border-color)',
                    boxShadow: isVipHop ? '0 0 20px rgba(245, 158, 11, 0.08)' : 'none',
                  }}
                >
                  {/* Top Bar of Card */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 14 }}>
                    <div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        {isVipHop && (
                          <span className="badge badge-vip">
                            <Sparkles size={12} />
                            {isRtl ? 'مسیر ۲: برندینگ VIP از C' : 'Route 2: VIP Hop via C'}
                          </span>
                        )}
                        {isDirectCopy && (
                          <span className="badge badge-direct">
                            <Copy size={12} />
                            {isRtl ? 'مسیر ۱: کپی تمیز مستقیم' : 'Route 1: Direct Clean Copy'}
                          </span>
                        )}
                        {isNative && (
                          <span className="badge badge-native">
                            <Send size={12} />
                            {isRtl ? 'مسیر ۳: فوروارد نیتیو' : 'Route 3: Native Forward'}
                          </span>
                        )}
                        <span className="badge badge-gray" style={{ fontSize: '0.75rem' }}>
                          P:{rule.priority}
                        </span>
                      </div>
                      <div
                        title={rule.id}
                        onClick={() => navigator.clipboard?.writeText(rule.id).catch(() => {})}
                        style={{
                          fontSize: '0.72rem',
                          color: '#8b95a7',
                          marginTop: 4,
                          fontFamily: 'monospace',
                          cursor: 'pointer',
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: 4,
                          padding: '1px 4px',
                          borderRadius: 4,
                          transition: 'background 0.15s',
                        }}
                        onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(255,255,255,0.06)' }}
                        onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent' }}
                      >
                        <Copy size={10} /> {rule.id.substring(0, 8)}…
                      </div>
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                      <button
                        onClick={() => handleToggleRule(rule.id)}
                        className="btn"
                        style={{
                          padding: '4px 8px',
                          fontSize: '0.75rem',
                          background: rule.is_active ? 'rgba(16, 185, 129, 0.15)' : 'rgba(239, 68, 68, 0.15)',
                          color: rule.is_active ? '#34d399' : '#f87171',
                          border: rule.is_active ? '1px solid rgba(16, 185, 129, 0.3)' : '1px solid rgba(239, 68, 68, 0.3)',
                        }}
                      >
                        {rule.is_active ? (isRtl ? 'فعال' : 'ACTIVE') : (isRtl ? 'غیرفعال' : 'PAUSED')}
                      </button>

                      {rule.is_paused ? (
                        <button
                          onClick={() => handleResumeRule(rule.id)}
                          className="btn btn-secondary"
                          style={{ padding: '4px 8px', fontSize: '0.75rem', color: '#34d399' }}
                          title={isRtl ? 'از سر گیری موقت' : 'Resume'}
                        >
                          <PlayCircle size={13} />
                          <span>{isRtl ? 'ادامه' : 'Resume'}</span>
                        </button>
                      ) : (
                        <button
                          onClick={() => handlePauseRule(rule.id)}
                          className="btn btn-secondary"
                          style={{ padding: '4px 8px', fontSize: '0.75rem', color: '#fbbf24' }}
                          title={isRtl ? 'توقف موقت' : 'Pause'}
                        >
                          <Clock size={13} />
                          <span>{isRtl ? 'توقف' : 'Pause'}</span>
                        </button>
                      )}

                      <button
                        onClick={() => {
                          setRuleFormData({ ...rule })
                          try {
                            setRuleMeta(JSON.parse((rule as any).custom_metadata_json || '{}'))
                          } catch {
                            setRuleMeta({})
                          }
                          setIsRuleModalOpen(true)
                        }}
                        className="btn btn-secondary"
                        style={{ padding: '6px' }}
                        title={isRtl ? 'ویرایش کامل' : 'Edit'}
                      >
                        <Edit3 size={14} />
                      </button>

                      <button
                        onClick={() => handleDeleteRule(rule.id)}
                        className="btn btn-danger"
                        style={{ padding: '6px' }}
                        title={isRtl ? 'حذف' : 'Delete'}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </div>

                  {/* Channel Flow Diagram */}
                  <div
                    style={{
                      background: 'rgba(15, 23, 42, 0.6)',
                      borderRadius: 10,
                      padding: '12px 14px',
                      marginBottom: 16,
                      border: '1px solid rgba(255, 255, 255, 0.05)',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6, fontSize: '0.82rem' }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.7rem' }}>{isRtl ? 'کانال مبدا (A)' : 'Source A'}</div>
                        <div style={{ fontWeight: 600, color: '#f8fafc', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {rule.source_chat_name || rule.source_chat_id}
                        </div>
                        <div style={{ fontSize: '0.7rem', color: '#64748b', fontFamily: 'monospace' }}>{rule.source_chat_id}</div>
                      </div>

                      {rule.use_intermediate ? (
                        <>
                          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: '#fbbf24' }}>
                            <span style={{ fontSize: '0.65rem' }}>Hop</span>
                            <ArrowRightLeft size={14} />
                          </div>
                          <div style={{ flex: 1, minWidth: 0, textAlign: 'center' }}>
                            <div style={{ color: '#fbbf24', fontSize: '0.7rem' }}>{isRtl ? 'واسط برند (C)' : 'Brand C'}</div>
                            <div style={{ fontWeight: 600, color: '#fbbf24', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                              {rule.intermediate_channel_name || rule.intermediate_channel_id || 'Channel C'}
                            </div>
                            <div style={{ fontSize: '0.7rem', color: '#b45309', fontFamily: 'monospace' }}>
                              {rule.intermediate_channel_id}
                            </div>
                          </div>
                          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: '#10b981' }}>
                            <span style={{ fontSize: '0.65rem' }}>Native</span>
                            <ChevronRight size={14} />
                          </div>
                        </>
                      ) : (
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: '#3b82f6', padding: '0 8px' }}>
                          <span style={{ fontSize: '0.65rem' }}>Direct</span>
                          <ChevronRight size={14} />
                        </div>
                      )}

                      <div style={{ flex: 1, minWidth: 0, textAlign: isRtl ? 'left' : 'right' }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.7rem' }}>{isRtl ? 'مقصد نهایی (B)' : 'Target B'}</div>
                        <div style={{ fontWeight: 600, color: '#f8fafc', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {rule.target_chat_name || rule.target_chat_id}
                        </div>
                        <div style={{ fontSize: '0.7rem', color: '#64748b', fontFamily: 'monospace' }}>{rule.target_chat_id}</div>
                      </div>
                    </div>
                  </div>

                  {/* Criteria & Details */}
                  <div style={{ fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 14 }}>
                    {rule.custom_header && (
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                        <span style={{ color: '#94a3b8' }}>{isRtl ? 'هدر پیام:' : 'Header:'}</span>
                        <code style={{ background: 'rgba(255,255,255,0.06)', padding: '2px 6px', borderRadius: 4, color: '#fbbf24' }}>
                          {rule.custom_header}
                        </code>
                      </div>
                    )}
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
                      <span className="badge badge-gray" style={{ fontSize: '0.7rem' }}>
                        {rule.link_policy}
                      </span>
                      {rule.filter_rule_id && (
                        <span className="badge badge-direct" style={{ fontSize: '0.7rem' }}>
                          <Filter size={10} /> Filter Rule
                        </span>
                      )}
                      {rule.ai_config_id && (
                        <span className="badge badge-vip" style={{ fontSize: '0.7rem' }}>
                          <Bot size={10} /> AI Enhanced
                        </span>
                      )}
                      {rule.fallback_enabled && (
                        <span className="badge badge-gray" style={{ fontSize: '0.7rem', color: '#a7f3d0' }}>
                          <Shield size={10} /> Fallback: {rule.fallback_mode}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Live per-rule counters from the delivery pipeline */}
                  {liveStat && (
                    <div
                      style={{
                        display: 'flex',
                        gap: 0,
                        marginBottom: 12,
                        borderRadius: 8,
                        overflow: 'hidden',
                        border: '1px solid rgba(255, 255, 255, 0.06)',
                        fontSize: '0.72rem',
                      }}
                    >
                      <div style={{ flex: 1, padding: '7px 8px', background: 'rgba(16, 185, 129, 0.10)', textAlign: 'center' }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.64rem', fontWeight: 600 }}>{isRtl ? 'فوروارد' : 'FWD'}</div>
                        <div style={{ fontWeight: 800, color: '#34d399', lineHeight: 1.35 }}>{liveStat.forwarded ?? 0}</div>
                      </div>
                      <div style={{ flex: 1, padding: '7px 8px', background: 'rgba(59, 130, 246, 0.10)', textAlign: 'center' }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.64rem', fontWeight: 600 }}>{isRtl ? 'فیلتر' : 'FILT'}</div>
                        <div style={{ fontWeight: 800, color: '#60a5fa', lineHeight: 1.35 }}>{liveStat.filtered ?? 0}</div>
                      </div>
                      <div style={{ flex: 1, padding: '7px 8px', background: liveStat.errors > 0 ? 'rgba(239, 68, 68, 0.18)' : 'rgba(239, 68, 68, 0.07)', textAlign: 'center' }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.64rem', fontWeight: 600 }}>{isRtl ? 'خطا' : 'ERR'}</div>
                        <div style={{ fontWeight: 800, color: liveStat.errors > 0 ? '#fca5a5' : '#64748b', lineHeight: 1.35 }}>{liveStat.errors ?? 0}</div>
                      </div>
                      <div style={{ flex: 1.4, padding: '7px 8px', background: 'rgba(99, 102, 241, 0.06)', textAlign: 'center' }}>
                        <div style={{ color: '#64748b', fontSize: '0.62rem' }}>{isRtl ? 'آخرین فوروارد' : 'LAST FWD'}</div>
                        <div style={{ fontWeight: 700, color: '#818cf8' }}>
                          {liveStat.last_forward_ts
                            ? new Date(liveStat.last_forward_ts * 1000).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })
                            : '—'}
                        </div>
                      </div>
                    </div>
                  )}
                  {liveStat?.last_error && (
                    <div
                      dir="ltr"
                      style={{
                        textAlign: 'left',
                        marginBottom: 12,
                        padding: '6px 10px',
                        borderRadius: 6,
                        background: 'rgba(239, 68, 68, 0.07)',
                        border: '1px solid rgba(239, 68, 68, 0.2)',
                        color: '#fca5a5',
                        fontSize: '0.7rem',
                        fontFamily: "'JetBrains Mono', Consolas, monospace",
                        wordBreak: 'break-word',
                      }}
                    >
                      ⚠️ {liveStat.last_error.slice(0, 160)}
                    </div>
                  )}

                  {/* 1-Click Route Switch Buttons */}
                  <div
                    style={{
                      borderTop: '1px solid rgba(255, 255, 255, 0.06)',
                      paddingTop: 12,
                      display: 'flex',
                      gap: 8,
                      flexWrap: 'wrap',
                    }}
                  >
                    <button
                      onClick={() => handleQuickRouteSwitch(rule.id, 'route1_direct')}
                      className={`btn ${isDirectCopy ? 'btn-primary' : 'btn-secondary'}`}
                      style={{ flex: 1, fontSize: '0.75rem', padding: '6px 8px' }}
                    >
                      <Copy size={12} />
                      <span>{isRtl ? 'مسیر ۱: مستقیم' : 'Route 1: Direct'}</span>
                    </button>

                    <button
                      onClick={() => handleQuickRouteSwitch(rule.id, 'route2_vip_hop')}
                      className={`btn ${isVipHop ? 'btn-vip' : 'btn-secondary'}`}
                      style={{ flex: 1, fontSize: '0.75rem', padding: '6px 8px' }}
                    >
                      <Sparkles size={12} />
                      <span>{isRtl ? 'مسیر ۲: VIP برند C' : 'Route 2: VIP Hop'}</span>
                    </button>

                    <button
                      onClick={() => handleQuickRouteSwitch(rule.id, 'route3_native')}
                      className={`btn ${isNative ? 'btn-primary' : 'btn-secondary'}`}
                      style={{ flex: 1, fontSize: '0.75rem', padding: '6px 8px' }}
                    >
                      <Send size={12} />
                      <span>{isRtl ? 'مسیر ۳: نیتیو' : 'Route 3: Native'}</span>
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* TAB 2: SMART SIMULATOR */}
      {activeTab === 'simulator' && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))', gap: 24 }}>
          {/* Controls Panel */}
          <div className="glass-panel" style={{ padding: 24 }}>
            <h2 style={{ fontSize: '1.2rem', fontWeight: 700, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
              <PlayCircle size={20} color="#fbbf24" />
              <span>{isRtl ? 'شبیه‌ساز تصمیم‌گیری و آزمون زنده' : 'Live Smart Simulator'}</span>
            </h2>
            <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginBottom: 20 }}>
              {isRtl
                ? 'ارزیابی رفتار موتور روتینگ، تطبیق VIP، بای‌پس محتوای قفل‌شده و پیش‌نمایش خروجی پیام قبل از ارسال واقعی'
                : 'Test routing engine decisions, VIP origin criteria, and protected content bypass'}
            </p>

            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                {isRtl ? 'انتخاب قانون برای شبیه‌سازی:' : 'Select Target Rule:'}
              </label>
              <select
                value={simSelectedRuleId}
                onChange={(e) => setSimSelectedRuleId(e.target.value)}
                className="input-field"
              >
                {(Array.isArray(rules) ? rules : []).map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.source_chat_name || r.source_chat_id} ➔ {r.target_chat_name || r.target_chat_id} (
                    {r.use_intermediate ? 'VIP Hop via C' : r.forward_mode})
                  </option>
                ))}
              </select>
            </div>

            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                {isRtl ? 'متن پیام آزمایشی:' : 'Simulated Message Text:'}
              </label>
              <textarea
                rows={4}
                value={simText}
                onChange={(e) => setSimText(e.target.value)}
                className="input-field"
                style={{ fontFamily: 'inherit', resize: 'vertical' }}
              />
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 16 }}>
              <div>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                  {isRtl ? 'شناسه منشأ فوروارد (Chat ID):' : 'Forward Origin Chat ID:'}
                </label>
                <input
                  type="text"
                  value={simOriginChatId}
                  onChange={(e) => setSimOriginChatId(e.target.value)}
                  className="input-field"
                  placeholder="-1001111111111"
                />
              </div>

              <div>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                  {isRtl ? 'نام نمایشی کانال منشأ:' : 'Forward Origin Title:'}
                </label>
                <input
                  type="text"
                  value={simOriginTitle}
                  onChange={(e) => setSimOriginTitle(e.target.value)}
                  className="input-field"
                  placeholder="Forex VIP Channel"
                />
              </div>
            </div>

            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                {isRtl ? 'یوزرنیم کانال منشأ (Username):' : 'Forward Origin Username:'}
              </label>
              <input
                type="text"
                value={simOriginUser}
                onChange={(e) => setSimOriginUser(e.target.value)}
                className="input-field"
                placeholder="vip_forex_signals"
              />
            </div>

            <div style={{ display: 'flex', gap: 16, marginBottom: 20 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: '0.85rem' }}>
                <input
                  type="checkbox"
                  checked={simIsProtected}
                  onChange={(e) => setSimIsProtected(e.target.checked)}
                />
                <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <Lock size={14} color="#f87171" />
                  {isRtl ? 'کانال منبع محافظت‌شده است (Protected/No-Forward)' : 'Protected Content Channel'}
                </span>
              </label>

              <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: '0.85rem' }}>
                <input
                  type="checkbox"
                  checked={simHasMedia}
                  onChange={(e) => setSimHasMedia(e.target.checked)}
                />
                <span>{isRtl ? 'پیام دارای تصویر/مدیا است' : 'Has Media'}</span>
              </label>
            </div>

            <button
              onClick={handleRunSimulation}
              disabled={simuring}
              className="btn btn-vip"
              style={{ width: '100%', padding: '12px' }}
            >
              <Zap size={18} />
              <span>{simuring ? (isRtl ? 'در حال شبیه‌سازی...' : 'Simulating...') : (isRtl ? 'اجرای ارزیابی هوشمند' : 'Run Decision Engine')}</span>
            </button>
          </div>

          {/* Results Panel */}
          <div className="glass-panel" style={{ padding: 24 }}>
            <h3 style={{ fontSize: '1.1rem', fontWeight: 700, marginBottom: 14 }}>
              {isRtl ? 'نتیجه تحلیل و جریان انتقال' : 'Engine Decision & Workflow Trace'}
            </h3>

            {simResult ? (
              <div>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '12px 16px',
                    borderRadius: 12,
                    background: simResult.matched
                      ? simResult.is_vip
                        ? 'rgba(245, 158, 11, 0.15)'
                        : 'rgba(59, 130, 246, 0.15)'
                      : 'rgba(239, 68, 68, 0.15)',
                    border: `1px solid ${
                      simResult.matched
                        ? simResult.is_vip
                          ? 'rgba(245, 158, 11, 0.4)'
                          : 'rgba(59, 130, 246, 0.4)'
                        : 'rgba(239, 68, 68, 0.4)'
                    }`,
                    marginBottom: 16,
                  }}
                >
                  <div>
                    <div style={{ fontSize: '0.75rem', opacity: 0.8 }}>{isRtl ? 'تصمیم پای نهایی' : 'Decision'}</div>
                    <div style={{ fontSize: '1.1rem', fontWeight: 800 }}>
                      {isRtl ? simResult.route_label_fa : simResult.route_label_en}
                    </div>
                  </div>
                  <span
                    className={`badge ${
                      simResult.matched ? (simResult.is_vip ? 'badge-vip' : 'badge-direct') : 'badge-gray'
                    }`}
                  >
                    {simResult.detected_category}
                  </span>
                </div>

                <div style={{ marginBottom: 16 }}>
                  <div style={{ fontSize: '0.8rem', color: '#94a3b8', marginBottom: 4 }}>
                    {isRtl ? 'دلیل تصمیم موتور:' : 'Decision Reason:'}
                  </div>
                  <div style={{ background: 'rgba(15, 23, 42, 0.6)', padding: '10px 14px', borderRadius: 8, fontSize: '0.85rem' }}>
                    {simResult.reason}
                  </div>
                </div>

                {simResult.protected_content_handled && (
                  <div
                    style={{
                      background: 'rgba(245, 158, 11, 0.12)',
                      border: '1px solid rgba(245, 158, 11, 0.3)',
                      borderRadius: 8,
                      padding: '10px 14px',
                      marginBottom: 16,
                      fontSize: '0.85rem',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      color: '#fbbf24',
                    }}
                  >
                    <Shield size={16} />
                    <span>
                      {isRtl
                        ? '🛡 بای‌پس حفاظت تلگرام: دانلود محتوای قفل‌شده و ارسال مجدد بدون نقض قوانین کپی‌رایت'
                        : 'Bypass Activated: Protected content will be re-uploaded directly.'}
                    </span>
                  </div>
                )}

                <div style={{ marginBottom: 16 }}>
                  <div style={{ fontSize: '0.8rem', color: '#94a3b8', marginBottom: 6 }}>
                    {isRtl ? 'گام‌های عملیاتی پایپ‌لاین:' : 'Pipeline Execution Steps:'}
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {(Array.isArray(simResult?.action_steps) ? simResult.action_steps : []).map((st, idx) => (
                      <div
                        key={idx}
                        style={{
                          background: 'rgba(15, 23, 42, 0.5)',
                          padding: '8px 12px',
                          borderRadius: 6,
                          fontSize: '0.8rem',
                          borderLeft: isRtl ? 'none' : '3px solid #818cf8',
                          borderRight: isRtl ? '3px solid #818cf8' : 'none',
                        }}
                      >
                        {st}
                      </div>
                    ))}
                  </div>
                </div>

                {simResult.preview_message && (
                  <div>
                    <div style={{ fontSize: '0.8rem', color: '#94a3b8', marginBottom: 6 }}>
                      {isRtl ? 'پیش‌نمایش خروجی پیام ارسالی به مقصد:' : 'Output Message Preview:'}
                    </div>
                    <pre
                      style={{
                        background: '#090d16',
                        padding: 14,
                        borderRadius: 8,
                        fontSize: '0.8rem',
                        whiteSpace: 'pre-wrap',
                        border: '1px solid rgba(255, 255, 255, 0.08)',
                        color: '#f8fafc',
                        fontFamily: 'inherit',
                      }}
                    >
                      {simResult.preview_message}
                    </pre>
                  </div>
                )}
              </div>
            ) : (
              <div style={{ textAlign: 'center', padding: '60px 20px', color: '#64748b' }}>
                <Activity size={36} style={{ margin: '0 auto 12px', opacity: 0.5 }} />
                <p>{isRtl ? 'هنوز شبیه‌سازی انجام نشده است. روی دکمه اجرای ارزیابی کلیک کنید.' : 'No simulation run yet. Click Run to evaluate.'}</p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* TAB 3: AI INTELLIGENCE */}
      {activeTab === 'ai' && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
            <div>
              <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>
                {isRtl ? 'پیکربندی هوش مصنوعی و بازنویسی خودکار' : 'AI Intelligence & Rewriting Engines'}
              </h2>
              <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                {isRtl
                  ? 'یکپارچه‌سازی با OpenAI، Anthropic و مدل‌های سفارشی جهت ترجمه، سیگنال‌یابی و بازنویسی'
                  : 'Configure LLM models for auto-rewriting, signal parsing, and translations'}
              </p>
            </div>
            <button
              onClick={() => {
                setAiFormData({
                  provider: 'OPENAI',
                  model: 'gpt-4o',
                  temperature: 0.7,
                  is_enabled: true,
                  target_language: 'fa',
                  system_prompt: 'You are an expert financial signal parser and translator.',
                  user_prompt_template: 'Translate and format this trade signal accurately:\n{text}',
                })
                setIsAiModalOpen(true)
              }}
              className="btn btn-primary"
            >
              <Plus size={16} />
              <span>{isRtl ? 'افزودن مدل هوش مصنوعی' : 'New AI Engine'}</span>
            </button>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 20 }}>
            {(Array.isArray(aiConfigs) ? aiConfigs : []).map((ai) => (
              <div key={ai.id} className="glass-panel" style={{ padding: 20 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
                  <div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <h3 style={{ fontSize: '1rem', fontWeight: 700 }}>{ai.name}</h3>
                      <span className="badge badge-vip">{ai.provider}</span>
                    </div>
                    <div style={{ fontSize: '0.8rem', color: '#818cf8', marginTop: 2, fontFamily: 'monospace' }}>
                      {ai.model}
                    </div>
                  </div>

                  <div style={{ display: 'flex', gap: 6 }}>
                    <button
                      onClick={() => {
                        setAiFormData({ ...ai })
                        setIsAiModalOpen(true)
                      }}
                      className="btn btn-secondary"
                      style={{ padding: '6px' }}
                    >
                      <Edit3 size={14} />
                    </button>
                    <button
                      onClick={() => handleDeleteAI(ai.id)}
                      className="btn btn-danger"
                      style={{ padding: '6px' }}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>

                <div style={{ fontSize: '0.8rem', color: '#cbd5e1', display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <div>
                    <span style={{ color: '#94a3b8' }}>API Key: </span>
                    <code>{ai.api_key_masked || 'None / Environment'}</code>
                  </div>
                  <div>
                    <span style={{ color: '#94a3b8' }}>Temperature: </span>
                    <span>{ai.temperature}</span> | <span style={{ color: '#94a3b8' }}>Lang: </span>
                    <span>{ai.target_language}</span>
                  </div>
                  {ai.system_prompt && (
                    <div style={{ background: 'rgba(15, 23, 42, 0.6)', padding: 8, borderRadius: 6, marginTop: 4 }}>
                      <span style={{ color: '#94a3b8', fontSize: '0.75rem', display: 'block' }}>System Prompt:</span>
                      <span style={{ fontSize: '0.75rem' }}>{ai.system_prompt}</span>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* TAB 4: ADVANCED FILTERS */}
      {activeTab === 'filters' && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
            <div>
              <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>
                {isRtl ? 'فیلترهای پیشرفته متن و رسانه' : 'Advanced Content & Media Filtering'}
              </h2>
              <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                {isRtl
                  ? 'تنظیم لیست‌های سفید/سیاه، الگوهای عبارات باقاعده (Regex) و مسدودسازی رسانه‌ها'
                  : 'Manage text blacklists/whitelists, Regex patterns, and media type policies'}
              </p>
            </div>
            <button
              onClick={() => {
                setFilterFormData({
                  whitelist_keywords: [],
                  blacklist_keywords: ['SPAM', 'JOIN', 'PROMO'],
                  regex_patterns: [],
                  allowed_media_types: ['PHOTO', 'VIDEO', 'DOCUMENT'],
                  blocked_media_types: ['STICKER', 'ANIMATION'],
                  drop_service_messages: true,
                  min_message_length: 5,
                  max_message_length: 4000,
                })
                setIsFilterModalOpen(true)
              }}
              className="btn btn-primary"
            >
              <Plus size={16} />
              <span>{isRtl ? 'افزودن فیلتر جدید' : 'New Filter Rule'}</span>
            </button>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 20 }}>
            {(Array.isArray(filterRules) ? filterRules : []).map((f) => (
              <div key={f.id} className="glass-panel" style={{ padding: 20 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
                  <h3 style={{ fontSize: '1rem', fontWeight: 700 }}>{f.name}</h3>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <button
                      onClick={() => {
                        setFilterFormData({ ...f })
                        setIsFilterModalOpen(true)
                      }}
                      className="btn btn-secondary"
                      style={{ padding: '6px' }}
                    >
                      <Edit3 size={14} />
                    </button>
                    <button
                      onClick={() => handleDeleteFilter(f.id)}
                      className="btn btn-danger"
                      style={{ padding: '6px' }}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>

                <div style={{ fontSize: '0.8rem', display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <div>
                    <span style={{ color: '#f87171', display: 'block', marginBottom: 2 }}>
                      {isRtl ? 'کلمات مسدود (Blacklist):' : 'Blacklist:'}
                    </span>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                      {(Array.isArray(f?.blacklist_keywords) ? f.blacklist_keywords : []).map((w, idx) => (
                        <span key={idx} style={{ background: 'rgba(239, 68, 68, 0.15)', color: '#fca5a5', padding: '2px 6px', borderRadius: 4 }}>
                          {w}
                        </span>
                      ))}
                    </div>
                  </div>

                  <div>
                    <span style={{ color: '#34d399', display: 'block', marginBottom: 2 }}>
                      {isRtl ? 'رسانه‌های مجاز:' : 'Allowed Media:'}
                    </span>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                      {(Array.isArray(f?.allowed_media_types) ? f.allowed_media_types : []).map((m, idx) => (
                        <span key={idx} style={{ background: 'rgba(16, 185, 129, 0.15)', color: '#6ee7b7', padding: '2px 6px', borderRadius: 4 }}>
                          {m}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* TAB 5: SESSIONS */}
      {activeTab === 'sessions' && (
        <div>
          <div style={{ marginBottom: 20 }}>
            <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>
              {isRtl ? 'سشن‌های متصل تلگرام (Userbots / Clients)' : 'Telegram MTProto Client Sessions'}
            </h2>
            <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
              {isRtl
                ? 'پایش حساب‌های تلگرام، وضعیت احراز هویت، پراکسی و کنترل نشست‌های فعال'
                : 'Monitor connected userbot sessions, auth status, and proxies'}
            </p>
          </div>

          <div className="sessions-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 20 }}>
            {(Array.isArray(sessions) ? sessions : []).map((sess) => (
              <div key={sess.id} className="glass-panel" style={{ padding: 20 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, gap: 8 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                    <Users size={18} color="#34d399" />
                    <span style={{ fontWeight: 700, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {sess.first_name || sess.username || sess.id}
                    </span>
                  </div>
                  <span className={`badge ${sess.is_active ? 'badge-native' : 'badge-gray'}`}>
                    {sess.is_active ? (isRtl ? 'متصل' : 'ONLINE') : (isRtl ? 'غیرفعال' : 'OFFLINE')}
                  </span>
                </div>

                <div style={{ fontSize: '0.8rem', color: '#cbd5e1', display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 14 }}>
                  <div>
                    <span style={{ color: '#94a3b8' }}>Phone: </span>
                    <span>{sess.phone_number || 'N/A'}</span>
                  </div>
                  <div>
                    <span style={{ color: '#94a3b8' }}>User ID: </span>
                    <span style={{ fontFamily: 'monospace' }}>{sess.user_id}</span>
                  </div>
                  <div>
                    <span style={{ color: '#94a3b8' }}>Auth Status: </span>
                    <span style={{ color: sess.is_authorized ? '#34d399' : '#f87171' }}>
                      {sess.is_authorized ? 'Authorized ✓' : 'Unauthorized ✗'}
                    </span>
                  </div>
                  <div>
                    <span style={{ color: '#94a3b8' }}>Proxy: </span>
                    <span>{sess.proxy || 'Direct (No Proxy)'}</span>
                  </div>
                </div>

                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', borderTop: '1px solid rgba(255,255,255,0.06)', paddingTop: 12 }}>
                  <button
                    onClick={() => handleBackupSession(sess)}
                    className="btn btn-secondary"
                    style={{ flex: 1, fontSize: '0.75rem', padding: '6px 8px', minWidth: 110 }}
                  >
                    <Layers size={13} />
                    <span>{isRtl ? 'بکاپ سشن' : 'Backup'}</span>
                  </button>
                  <button
                    onClick={() => handleTerminateSession(sess)}
                    className="btn btn-danger"
                    style={{ flex: 1, fontSize: '0.75rem', padding: '6px 8px', minWidth: 110 }}
                  >
                    <X size={13} />
                    <span>{isRtl ? 'قطع سشن' : 'Terminate'}</span>
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* TAB 6: QUEUE & DLQ */}
      {activeTab === 'queue' && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
            <div>
              <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>
                {isRtl ? 'پایپ‌لاین ارسال و صف خطاهای DLQ' : 'Delivery Jobs & Dead Letter Queue (DLQ)'}
              </h2>
              <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                {isRtl
                  ? 'بررسی جاب‌های در حال انجام، خطاهای پایپ‌لاین و تلاش مجدد برای پیام‌های شکست‌خورده'
                  : 'Track in-flight delivery jobs and recover failed telegram messages'}
              </p>
            </div>
            {dlqJobs.length > 0 && (
              <button onClick={handlePurgeDLQ} className="btn btn-danger">
                <Trash2 size={16} />
                <span>{isRtl ? 'تخلیه صف خطاهای DLQ' : 'Purge All DLQ'}</span>
              </button>
            )}
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))', gap: 24 }}>
            {/* Active Delivery Queue */}
            <div className="glass-panel" style={{ padding: 20 }}>
              <h3 style={{ fontSize: '1rem', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                <Clock size={16} color="#818cf8" />
                <span>{isRtl ? 'جاب‌های فعال در حال تحویل' : 'Recent Delivery Jobs'}</span>
              </h3>

              {queueJobs.length === 0 ? (
                <div style={{ textAlign: 'center', padding: 30, color: '#64748b', fontSize: '0.85rem' }}>
                  {isRtl ? 'هیچ جابی در صف معلق نیست (همه تحویل شدند)' : 'No pending jobs in queue'}
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {(Array.isArray(queueJobs) ? queueJobs : []).map((q) => (
                    <div
                      key={q.id}
                      style={{
                        background: 'rgba(15, 23, 42, 0.6)',
                        padding: '10px 14px',
                        borderRadius: 8,
                        fontSize: '0.8rem',
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                      }}
                    >
                      <div>
                        <div style={{ fontWeight: 600 }}>Msg #{q.source_message_id}</div>
                        <div style={{ color: '#94a3b8', fontSize: '0.75rem' }}>Stage: {q.delivery_stage}</div>
                      </div>
                      <span className="badge badge-direct">{q.status}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Dead Letter Queue */}
            <div className="glass-panel" style={{ padding: 20 }}>
              <h3 style={{ fontSize: '1rem', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                <Activity size={16} color="#ef4444" />
                <span>{isRtl ? 'صف پیام‌های ناموفق (DLQ)' : 'Dead Letter Queue (DLQ)'}</span>
                <span className="badge badge-vip">{dlqJobs.length}</span>
              </h3>

              {dlqJobs.length === 0 ? (
                <div style={{ textAlign: 'center', padding: 30, color: '#64748b', fontSize: '0.85rem' }}>
                  {isRtl ? 'صف خطاهای DLQ خالی است ✓' : 'Dead Letter Queue is empty ✓'}
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {(Array.isArray(dlqJobs) ? dlqJobs : []).map((dlq) => (
                    <div
                      key={dlq.id}
                      style={{
                        background: 'rgba(15, 23, 42, 0.6)',
                        padding: '12px 14px',
                        borderRadius: 8,
                        fontSize: '0.8rem',
                        border: '1px solid rgba(239, 68, 68, 0.3)',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                        <span style={{ fontWeight: 700, color: '#fca5a5' }}>{dlq.error_category}</span>
                        <button
                          onClick={() => handleRetryDLQ(dlq.id)}
                          className="btn btn-secondary"
                          style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                        >
                          <RefreshCw size={12} />
                          <span>{isRtl ? 'تلاش مجدد' : 'Retry'}</span>
                        </button>
                      </div>
                      <div style={{ color: '#cbd5e1', fontSize: '0.75rem' }}>{dlq.last_error}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* TAB 7: DEVOPS GATEWAY & PRO ACTIVE LOGGER */}
      {activeTab === 'devops' && (
        <div>
          <div style={{ marginBottom: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12 }}>
            <div>
              <h2 style={{ fontSize: '1.25rem', fontWeight: 800, letterSpacing: '-0.02em', display: 'flex', alignItems: 'center', gap: 10 }}>
                <Terminal size={22} color="#818cf8" />
                <span>{isRtl ? 'مرکز لاگر پیشرفته و مانیتورینگ زنده (Pro Active Logger)' : 'Pro Active Logger & Diagnostics'}</span>
              </h2>
              <p style={{ fontSize: '0.825rem', color: '#94a3b8', marginTop: 4 }}>
                {isRtl
                  ? 'سرویس لاگر مستقل بر بستر gRPC پورت 6002 با پایگاه داده SQLite WAL، فیلترینگ چندسطحی و استریم زنده'
                  : 'High-throughput independent Logger microservice on gRPC :6002 with live auto-refresh and multi-tier filtering'}
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
                    background: logAutoRefresh ? '#10b981' : '#64748b',
                    boxShadow: logAutoRefresh ? '0 0 8px #10b981' : 'none',
                  }}
                />
                <span>{logAutoRefresh ? (isRtl ? 'استریم زنده فعال (۳ ثانیه)' : 'Live Stream Active (3s)') : (isRtl ? 'استریم متوقف' : 'Live Stream Paused')}</span>
              </button>

              <button
                onClick={() => fetchLogs()}
                className="btn btn-secondary"
                style={{ padding: '6px 12px', fontSize: '0.8rem' }}
                title={isRtl ? 'بروزرسانی دستی' : 'Refresh Now'}
              >
                <RefreshCw size={14} />
                <span>{isRtl ? 'بروزرسانی' : 'Refresh'}</span>
              </button>
            </div>
          </div>

          {/* Microservices Topology & Statistics */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 14, marginBottom: 20 }}>
            <div className="glass-panel" style={{ padding: 16 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>ATF Logger Microservice</span>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#10b981', boxShadow: '0 0 8px #10b981' }} />
              </div>
              <div style={{ fontSize: '1.25rem', fontWeight: 800, color: '#34d399', marginTop: 4 }}>
                Port 6002 <span style={{ fontSize: '0.8rem', color: '#94a3b8', fontWeight: 500 }}>gRPC</span>
              </div>
              <div style={{ fontSize: '0.75rem', color: '#64748b', marginTop: 4 }}>
                {logStats ? `${logStats.total} ${isRtl ? 'لاگ ثبت‌شده' : 'events logged'}` : 'SQLite WAL Engine'}
              </div>
            </div>

            <div className="glass-panel" style={{ padding: 16 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>Python Core & MTProto</span>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: stats?.atf_core_online ? '#10b981' : '#ef4444', boxShadow: '0 0 8px #10b981' }} />
              </div>
              <div style={{ fontSize: '1.25rem', fontWeight: 800, color: '#38bdf8', marginTop: 4 }}>
                Port 6001 <span style={{ fontSize: '0.8rem', color: '#94a3b8', fontWeight: 500 }}>Core</span>
              </div>
              <div style={{ fontSize: '0.75rem', color: '#64748b', marginTop: 4 }}>systemctl: atf.service</div>
            </div>

            <div className="glass-panel" style={{ padding: 16 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>Go REST Control Plane</span>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#10b981', boxShadow: '0 0 8px #10b981' }} />
              </div>
              <div style={{ fontSize: '1.25rem', fontWeight: 800, color: '#a855f7', marginTop: 4 }}>
                Port 8080 <span style={{ fontSize: '0.8rem', color: '#94a3b8', fontWeight: 500 }}>REST</span>
              </div>
              <div style={{ fontSize: '0.75rem', color: '#64748b', marginTop: 4 }}>systemctl: atf-api.service</div>
            </div>

            <div className="glass-panel" style={{ padding: 16 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>Rust Axum Web Gateway</span>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#10b981', boxShadow: '0 0 8px #10b981' }} />
              </div>
              <div style={{ fontSize: '1.25rem', fontWeight: 800, color: '#f59e0b', marginTop: 4 }}>
                Port 8088 <span style={{ fontSize: '0.8rem', color: '#94a3b8', fontWeight: 500 }}>v{gatewayInfo?.version ?? '1.2.2'}</span>
              </div>
              <div style={{ fontSize: '0.75rem', color: '#64748b', marginTop: 4 }}>systemctl: atf-web.service</div>
            </div>
          </div>

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
                {/* Service Filter */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{isRtl ? 'سرویس:' : 'Service:'}</span>
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
                    <option value="all">{isRtl ? 'همه سرویس‌ها' : 'All Services'}</option>
                    <option value="core">core (پایتون هسته)</option>
                    <option value="bot">bot (ربات تلگرام)</option>
                    <option value="api">api (کنترل پلین Go)</option>
                    <option value="pipeline">pipeline (پایپ‌لاین فوروارد)</option>
                    <option value="boot">boot (راه‌اندازی سشن‌ها)</option>
                  </select>
                </div>

                {/* Level Filter */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{isRtl ? 'سطح لاگ:' : 'Level:'}</span>
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
                    <option value="all">{isRtl ? 'همه سطوح' : 'All Levels'}</option>
                    <option value="ERROR">❌ ERROR</option>
                    <option value="WARN">⚠️ WARN</option>
                    <option value="INFO">ℹ️ INFO</option>
                    <option value="DEBUG">🔍 DEBUG</option>
                  </select>
                </div>
              </div>

              {/* Text Search Input */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 260, flex: 1, maxWidth: 380 }}>
                <input
                  type="text"
                  placeholder={isRtl ? 'جستجوی زنده در پیام و متن لاگ...' : 'Search logs live...'}
                  value={logSearch}
                  onChange={(e) => {
                    setLogSearch(e.target.value)
                    fetchLogs(logFilterService, logFilterLevel, e.target.value)
                  }}
                  className="input-field"
                  style={{ padding: '6px 12px', fontSize: '0.8rem' }}
                />
                {logSearch && (
                  <button
                    onClick={() => {
                      setLogSearch('')
                      fetchLogs(logFilterService, logFilterLevel, '')
                    }}
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
                {isRtl ? 'تعداد نمایش داده شده:' : 'Showing:'} {Array.isArray(logs) ? logs.length : 0}
              </span>
              {logStats && (
                <>
                  <span className="badge" style={{ background: 'rgba(16, 185, 129, 0.15)', color: '#34d399' }}>
                    INFO: {logStats.by_level?.INFO ?? 0}
                  </span>
                  <span className="badge" style={{ background: 'rgba(245, 158, 11, 0.15)', color: '#fbbf24' }}>
                    WARN: {logStats.by_level?.WARN ?? 0}
                  </span>
                  <span className="badge" style={{ background: 'rgba(239, 68, 68, 0.15)', color: '#f87171' }}>
                    ERROR: {logStats.by_level?.ERROR ?? 0}
                  </span>
                  <span className="badge badge-vip">
                    TOTAL ARCHIVE: {logStats.total}
                  </span>
                </>
              )}
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
              maxWidth: 680,
              maxHeight: '90vh',
              overflowY: 'auto',
              padding: 28,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
              <h3 style={{ fontSize: '1.2rem', fontWeight: 800 }}>
                {ruleFormData.id ? (isRtl ? 'ویرایش قانون فوروارد' : 'Edit Forward Rule') : (isRtl ? 'قانون فوروارد جدید' : 'New Forward Rule')}
              </h3>
              <button onClick={() => setIsRuleModalOpen(false)} className="btn btn-secondary" style={{ padding: 6 }}>
                <X size={16} />
              </button>
            </div>

            <form onSubmit={handleSaveRule}>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
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

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
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

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 24 }}>
                <button type="button" onClick={() => setIsRuleModalOpen(false)} className="btn btn-secondary">
                  {isRtl ? 'انصراف' : 'Cancel'}
                </button>
                <button type="submit" className="btn btn-primary">
                  <Check size={16} />
                  <span>{isRtl ? 'ذخیره قانون' : 'Save Rule'}</span>
                </button>
              </div>
            </form>
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
