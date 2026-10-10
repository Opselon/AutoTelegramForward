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
  PVResponderConfig,
} from './types'

export function App() {
  const [lang, setLang] = useState<'fa' | 'en'>('fa')
  const [activeTab, setActiveTab] = useState<'rules' | 'simulator' | 'ai' | 'pv' | 'filters' | 'sessions' | 'queue' | 'devops'>('rules')
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
  const [dismissedErrors, setDismissedErrors] = useState<Set<string>>(() => {
    try {
      const saved = sessionStorage.getItem('atf_dismissed_errors')
      return saved ? new Set(JSON.parse(saved)) : new Set()
    } catch {
      return new Set()
    }
  })
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
      const [r, s, ai, f, q, dlq, gw, st, l, ds, pv] = await Promise.all([
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
        api.getPVResponder().catch(() => null),
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

  const PV_PRESETS = {
    casual: {
      label_fa: '😃 خودمانی و صمیمی (پیش‌فرض)',
      label_en: '😃 Friendly & Natural (Default)',
      prompt: `شما صاحب این اکانت شخصی تلگرام هستید و دارید مستقیماً به پیام‌های شخصی (پی‌وی) مخاطبان و دوستان خود پاسخ می‌دهید.

قوانین حیاتی و شیوه صحبت کردن:
۱. لحن شما باید کاملاً خودمانی، بسیار جذاب، صمیمی، محترمانه و زنده به زبان فارسی باشد؛ دقیقاً مانند یک جوان هوشمند، خوش‌برخورد و امروزی ایرانی در چت تلگرام.
۲. هرگز و تحت هیچ شرایطی نگو که ربات، هوش مصنوعی، مدل زبانی یا دستیار مجازی هستی! وانمود کن که خود کاربر هستی.
۳. پاسخ‌ها کوتاه، صمیمانه، خوش‌طبع و موجز باشند (از پاسخ‌های طولانی، رسمی یا مقاله‌ای پرهیز کن).
۴. از اموجی‌های متناسب و طبیعی چت (مانند 🙏، 😉، 🤝، 😂، ✌️) به‌جا و به اندازه استفاده کن.
۵. اگر سوال فنی یا کاری پرسیدند، هوشمندانه و کوتاه راهنمایی کن. اگر گفت‌وگوی احوال‌پرسی است، گرم و صمیمی باش.`,
    },
    business: {
      label_fa: '💼 کاری، مؤدبانه و رسمی',
      label_en: '💼 Professional & Polite',
      prompt: `شما صاحب این اکانت شخصی تلگرام هستید. پاسخ‌های شما در پی‌وی به مراجعین و همکاران باید بسیار مؤدبانه، حرفه‌ای، محترمانه و سازنده باشد.
قوانین:
۱. از زبان فارسی شیک و کلمات محترمانه استفاده کنید.
۲. تحت هیچ عنوان هویت ربات یا هوش مصنوعی را بروز ندهید؛ خود شخص پشت این اکانت هستید.
۳. پاسخ‌ها دقیق، روشن و با رعایت نزاکت کاری باشند.`,
    },
    short: {
      label_fa: '⚡ پاسخ‌های فوق‌العاده کوتاه',
      label_en: '⚡ Ultra-Short & Casual',
      prompt: `شما صاحب این اکانت تلگرام هستید. پاسخ‌ها بسیار کوتاه (حداکثر ۱ یا ۲ جمله)، کاملاً عامیانه، صمیمی و شبیه چت سریع جوانان در تلگرام باشد. هیچ مقدمه، موخوره یا توضیحات اضافه نده. هرگز نگو ربات هستی!`,
    },
  }

  const handleSavePV = async (updated?: Partial<PVResponderConfig>) => {
    setPvSaving(true)
    try {
      const cfg: PVResponderConfig = {
        enabled: pvConfig?.enabled ?? false,
        persona_prompt: pvConfig?.persona_prompt || PV_PRESETS.casual.prompt,
        typing_delay_min: pvConfig?.typing_delay_min ?? 2.0,
        typing_delay_max: pvConfig?.typing_delay_max ?? 4.5,
        cooldown_seconds: pvConfig?.cooldown_seconds ?? 15,
        ignore_bots: pvConfig?.ignore_bots ?? true,
        history_limit: pvConfig?.history_limit ?? 4,
        ai_config_id: pvConfig?.ai_config_id || undefined,
        ...updated,
      }
      const saved = await api.savePVResponder(cfg)
      setPvConfig(saved)
      showToast(isRtl ? 'تنظیمات دستیار پی‌وی ذخیره شد' : 'PV Assistant config saved successfully')
    } catch (e: any) {
      showToast(e.message || 'Error saving PV Assistant config')
    } finally {
      setPvSaving(false)
    }
  }

  const handleTestPVSimulate = async () => {
    if (!pvTestText.trim()) return
    setPvTesting(true)
    setPvTestReply(null)
    try {
      const delay = Math.min(Math.max((pvConfig?.typing_delay_min || 2.0) * 1000, 1500), 4000)
      await new Promise((res) => setTimeout(res, delay))
      const resp = await api.simulateAIRewrite({
        text: pvTestText,
        system_prompt: pvConfig?.persona_prompt || PV_PRESETS.casual.prompt,
        config_id: pvConfig?.ai_config_id || undefined,
      }).catch(() => null)
      if (resp?.rewritten_text) {
        setPvTestReply(resp.rewritten_text)
      } else {
        setPvTestReply('سلام داداش، در خدمتم! شرایط همکاری رو برات می‌فرستم، هر سوالی بود بگو تا با هم چکش کنیم 🙏')
      }
    } catch {
      setPvTestReply('سلام، ممنون از پیامت! در اولین فرصت پاسخ می‌دم 🙏')
    } finally {
      setPvTesting(false)
    }
  }

  const handleSaveRule = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      if (!ruleFormData.source_chat_id || !ruleFormData.target_chat_id) {
        showToast(isRtl ? 'لطفا شناسه‌های مبدا و مقصد را وارد کنید' : 'Source and target IDs are required')
        return
      }
      const s = (ruleFormData.source_chat_id || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
      const t = (ruleFormData.target_chat_id || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
      if (s === t) {
        showToast(isRtl ? 'خطای حلقه (Loop Detected): شناسه کانال مبدأ و مقصد یکسان است!' : 'Routing Loop Detected: Source and target IDs cannot be identical!')
        return
      }
      if (ruleFormData.use_intermediate && ruleFormData.intermediate_channel_id) {
        const im = (ruleFormData.intermediate_channel_id || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
        if (im === s || im === t) {
          showToast(isRtl ? 'خطای حلقه: کانال واسط نمی‌تواند با مبدأ یا مقصد یکسان باشد!' : 'Routing Loop Detected: Intermediate channel cannot match source or target!')
          return
        }
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
      let msg = e.message || 'Error'
      if (msg.includes('loop_detected') || msg.includes('Invalid rule endpoints')) {
        msg = isRtl
          ? 'خطای حلقه (Loop Detected): شناسه کانال مبدأ و مقصد یکسان است و امکان ارسال به مبدأ وجود ندارد'
          : 'Routing Loop Detected: Source and target endpoints cannot be identical'
      }
      showToast(msg)
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
    <div dir={isRtl ? 'rtl' : 'ltr'} className="app-container" style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
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
            onClick={() => {
              if (errorBanner) {
                setDismissedErrors((prev) => {
                  const next = new Set(prev).add(errorBanner)
                  try {
                    sessionStorage.setItem('atf_dismissed_errors', JSON.stringify([...next]))
                  } catch {}
                  return next
                })
              }
              setErrorBanner(null)
            }}
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
        className="glass-panel header-container"
        style={{
          padding: '16px 20px',
          marginBottom: 20,
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 14,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div
            style={{
              width: 42,
              height: 42,
              borderRadius: 12,
              background: 'linear-gradient(135deg, #6366f1 0%, #a855f7 100%)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              boxShadow: '0 4px 15px rgba(99, 102, 241, 0.4)',
              flexShrink: 0,
            }}
          >
            <Radio size={22} color="#fff" />
          </div>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <h1 style={{ fontSize: '1.15rem', fontWeight: 800, letterSpacing: '-0.02em', margin: 0 }}>
                AutoTelegramForward <span style={{ color: '#818cf8', fontWeight: 600 }}>PRO GATEWAY</span>
              </h1>
              <span className="badge badge-vip" style={{ fontSize: '0.72rem', padding: '2px 8px' }}>
                v{gatewayInfo?.version ?? '1.2.2'} RELEASE
              </span>
            </div>
            <p className="text-balance" style={{ fontSize: '0.78rem', color: '#94a3b8', marginTop: 2, margin: 0 }}>
              {isRtl
                ? 'سامانه یکپارچه فوروارد هوشمند، میکروسرویس Rust + React و درگاه DevOps'
                : 'Unified Smart Telegram Forwarding Gateway & Microservice Platform'}
            </p>
          </div>
        </div>

        {/* Microservice Health Indicators */}
        <div className="header-actions" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              background: 'rgba(15, 23, 42, 0.6)',
              padding: '5px 10px',
              borderRadius: 8,
              border: '1px solid rgba(255, 255, 255, 0.06)',
              fontSize: '0.75rem',
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: '50%',
                background: stats?.atf_core_online ? '#10b981' : '#ef4444',
                boxShadow: stats?.atf_core_online ? '0 0 8px #10b981' : '0 0 8px #ef4444',
              }}
            />
            <span style={{ color: '#cbd5e1' }}>Core:6001</span>
          </div>

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              background: 'rgba(15, 23, 42, 0.6)',
              padding: '5px 10px',
              borderRadius: 8,
              border: '1px solid rgba(255, 255, 255, 0.06)',
              fontSize: '0.75rem',
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: '50%',
                background: stats?.atf_logger_online ? '#10b981' : '#ef4444',
                boxShadow: stats?.atf_logger_online ? '0 0 8px #10b981' : '0 0 8px #ef4444',
              }}
            />
            <span style={{ color: '#cbd5e1' }}>Log:6002</span>
          </div>

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              background: 'rgba(15, 23, 42, 0.6)',
              padding: '5px 10px',
              borderRadius: 8,
              border: '1px solid rgba(255, 255, 255, 0.06)',
              fontSize: '0.75rem',
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: '50%',
                background: '#10b981',
                boxShadow: '0 0 8px #10b981',
              }}
            />
            <span style={{ color: '#cbd5e1' }}>Web:8088</span>
          </div>

          {/* Language Toggle */}
          <button
            onClick={() => setLang(lang === 'fa' ? 'en' : 'fa')}
            className="btn btn-secondary"
            style={{ padding: '5px 10px', fontSize: '0.78rem' }}
          >
            <Globe size={13} />
            <span>{lang === 'fa' ? 'English' : 'فارسی'}</span>
          </button>

          {/* Reload Button */}
          <button
            onClick={loadData}
            disabled={loading}
            className="btn btn-secondary"
            style={{ padding: '5px 10px', fontSize: '0.78rem' }}
            title={isRtl ? 'به‌روزرسانی داده‌ها' : 'Refresh'}
          >
            <RotateCcw size={13} />
          </button>
        </div>
      </header>

      {/* Overview Stat Badges */}
      <div
        className="overview-grid"
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
          gap: 14,
          marginBottom: 20,
        }}
      >
        <div className="glass-panel overview-card" style={{ padding: '14px 16px', display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ padding: 8, borderRadius: 10, background: 'rgba(99, 102, 241, 0.15)', color: '#818cf8', flexShrink: 0 }}>
            <Layers size={20} />
          </div>
          <div>
            <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>{isRtl ? 'کل قوانین فعال' : 'Active Rules'}</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 800 }}>
              {stats?.active_rules ?? 0} <span style={{ fontSize: '0.8rem', color: '#64748b' }}>/ {stats?.total_rules ?? 0}</span>
            </div>
          </div>
        </div>

        <div className="glass-panel overview-card" style={{ padding: '14px 16px', display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ padding: 8, borderRadius: 10, background: 'rgba(245, 158, 11, 0.15)', color: '#fbbf24', flexShrink: 0 }}>
            <Send size={20} />
          </div>
          <div>
            <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>{isRtl ? 'فوروارد ۲۴ ساعت' : 'Forwarded 24h'}</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 800, color: '#fbbf24' }}>
              {stats?.total_forwarded_24h ?? 0}
            </div>
          </div>
        </div>

        <div className="glass-panel overview-card" style={{ padding: '14px 16px', display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ padding: 8, borderRadius: 10, background: 'rgba(16, 185, 129, 0.15)', color: '#34d399', flexShrink: 0 }}>
            <Users size={20} />
          </div>
          <div>
            <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>{isRtl ? 'سشن‌های متصل' : 'Connected Sessions'}</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 800 }}>
              {stats?.active_sessions ?? 0} <span style={{ fontSize: '0.8rem', color: '#64748b' }}>/ {stats?.total_sessions ?? 0}</span>
            </div>
          </div>
        </div>

        <div className="glass-panel overview-card" style={{ padding: '14px 16px', display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ padding: 8, borderRadius: 10, background: 'rgba(239, 68, 68, 0.15)', color: '#f87171', flexShrink: 0 }}>
            <Activity size={20} />
          </div>
          <div>
            <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>{isRtl ? 'صف خطاها (DLQ)' : 'Dead Letter Queue'}</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 800, color: dlqJobs.length > 0 ? '#ef4444' : '#94a3b8' }}>
              {dlqJobs.length}
            </div>
          </div>
        </div>
      </div>

      {/* Tabs Navigation */}
      <div
        className="tab-strip no-scrollbar"
        style={{
          display: 'flex',
          gap: 8,
          borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
          paddingBottom: 10,
          marginBottom: 20,
          overflowX: 'auto',
          WebkitOverflowScrolling: 'touch',
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
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <Radio size={15} />
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
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <PlayCircle size={15} />
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
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <Bot size={15} />
          <span>{isRtl ? 'هوش مصنوعی' : 'AI Intelligence'}</span>
          <span style={{ fontSize: '0.75rem', opacity: 0.7 }}>({aiConfigs.length})</span>
        </button>

        <button
          onClick={() => setActiveTab('pv')}
          className="btn"
          style={{
            background: activeTab === 'pv' ? 'rgba(236, 72, 153, 0.2)' : 'transparent',
            color: activeTab === 'pv' ? '#f472b6' : '#94a3b8',
            border: activeTab === 'pv' ? '1px solid rgba(236, 72, 153, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'pv' ? 700 : 500,
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <MessageSquare size={15} />
          <span>{isRtl ? 'دستیار پی‌وی' : 'PV Assistant'}</span>
          {pvConfig?.enabled && (
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: '#10b981', boxShadow: '0 0 6px #10b981' }} />
          )}
        </button>

        <button
          onClick={() => setActiveTab('filters')}
          className="btn"
          style={{
            background: activeTab === 'filters' ? 'rgba(59, 130, 246, 0.2)' : 'transparent',
            color: activeTab === 'filters' ? '#60a5fa' : '#94a3b8',
            border: activeTab === 'filters' ? '1px solid rgba(59, 130, 246, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'filters' ? 700 : 500,
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <Filter size={15} />
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
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <Users size={15} />
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
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <Inbox size={15} />
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
            color: activeTab === 'devops' ? '#cbd5e1' : '#94a3b8',
            border: activeTab === 'devops' ? '1px solid rgba(148, 163, 184, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'devops' ? 700 : 500,
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <Server size={15} />
          <span>{isRtl ? 'درگاه DevOps' : 'DevOps & Logs'}</span>
        </button>
      </div>

      {/* TAB 1: RULES & ROUTES */}
      {activeTab === 'rules' && (
        <div>
          <div className="hero-container" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
            <div>
              <h2 style={{ fontSize: '1.2rem', fontWeight: 800, margin: 0 }}>
                {isRtl ? 'قوانین فوروارد و مسیریابی هوشمند' : 'Smart Forwarding & Routing Rules'}
              </h2>
              <p className="text-balance" style={{ fontSize: '0.82rem', color: '#94a3b8', marginTop: 4, margin: 0 }}>
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
              className="hero-add-btn btn btn-primary"
            >
              <Plus size={16} />
              <span>{isRtl ? 'افزودن قانون جدید +' : 'New Rule +'}</span>
            </button>
          </div>

          <div className="rules-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 16 }}>
            {(Array.isArray(rules) ? rules : []).map((rule) => {
              const isVipHop = rule.use_intermediate && rule.message_category === 'VIP_ONLY'
              const isDirectCopy = !rule.use_intermediate && rule.forward_mode !== 'DIRECT_FORWARD'
              const isNative = !rule.use_intermediate && rule.forward_mode === 'DIRECT_FORWARD'
              const liveStat = (ruleLiveStats || []).find((st) => st.rule_id === rule.id)

              return (
                <div
                  key={rule.id}
                  className="glass-panel rule-card"
                  style={{
                    padding: '16px',
                    position: 'relative',
                    border: isVipHop ? '1px solid rgba(245, 158, 11, 0.4)' : '1px solid var(--border-color)',
                    boxShadow: isVipHop ? '0 0 20px rgba(245, 158, 11, 0.08)' : 'none',
                  }}
                >
                  {/* Top Bar of Card */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12, gap: 8, flexWrap: 'wrap' }}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                        {isVipHop && (
                          <span className="badge badge-vip" style={{ fontSize: '0.72rem', padding: '3px 8px' }}>
                            <Sparkles size={11} />
                            {isRtl ? 'مسیر ۲: برندینگ VIP' : 'Route 2: VIP Hop'}
                          </span>
                        )}
                        {isDirectCopy && (
                          <span className="badge badge-direct" style={{ fontSize: '0.72rem', padding: '3px 8px' }}>
                            <Copy size={11} />
                            {isRtl ? 'مسیر ۱: کپی مستقیم' : 'Route 1: Direct Copy'}
                          </span>
                        )}
                        {isNative && (
                          <span className="badge badge-native" style={{ fontSize: '0.72rem', padding: '3px 8px' }}>
                            <Send size={11} />
                            {isRtl ? 'مسیر ۳: نیتیو' : 'Route 3: Native'}
                          </span>
                        )}
                        {rule.source_chat_id && rule.target_chat_id &&
                         rule.source_chat_id.trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '') ===
                         rule.target_chat_id.trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '') && (
                          <span
                            className="badge"
                            style={{
                              background: 'rgba(239, 68, 68, 0.2)',
                              color: '#fca5a5',
                              border: '1px solid rgba(239, 68, 68, 0.5)',
                              fontSize: '0.7rem',
                            }}
                          >
                            <Shield size={11} color="#ef4444" />
                            {isRtl ? 'خطای حلقه' : 'Loop'}
                          </span>
                        )}
                        <span className="badge badge-gray" style={{ fontSize: '0.7rem', padding: '2px 6px' }}>
                          P:{rule.priority}
                        </span>
                      </div>
                      <div
                        title={rule.id}
                        onClick={() => navigator.clipboard?.writeText(rule.id).catch(() => {})}
                        style={{
                          fontSize: '0.7rem',
                          color: '#8b95a7',
                          cursor: 'pointer',
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: 4,
                          padding: '1px 4px',
                          borderRadius: 4,
                        }}
                      >
                        <Copy size={10} /> <span className="font-mono-ltr">{rule.id.substring(0, 8)}…</span>
                      </div>
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                      <button
                        onClick={() => handleToggleRule(rule.id)}
                        className="btn"
                        style={{
                          padding: '4px 8px',
                          fontSize: '0.72rem',
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
                          style={{ padding: '4px 8px', fontSize: '0.72rem', color: '#34d399' }}
                          title={isRtl ? 'از سر گیری' : 'Resume'}
                        >
                          <PlayCircle size={12} />
                          <span>{isRtl ? 'ادامه' : 'Resume'}</span>
                        </button>
                      ) : (
                        <button
                          onClick={() => handlePauseRule(rule.id)}
                          className="btn btn-secondary"
                          style={{ padding: '4px 8px', fontSize: '0.72rem', color: '#fbbf24' }}
                          title={isRtl ? 'توقف' : 'Pause'}
                        >
                          <Clock size={12} />
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
                        style={{ padding: '5px' }}
                        title={isRtl ? 'ویرایش' : 'Edit'}
                      >
                        <Edit3 size={13} />
                      </button>

                      <button
                        onClick={() => handleDeleteRule(rule.id)}
                        className="btn btn-danger"
                        style={{ padding: '5px' }}
                        title={isRtl ? 'حذف' : 'Delete'}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </div>

                  {/* Channel Flow Diagram — Mobile (Vertical Stack) */}
                  <div className="channel-flow-mobile" style={{ background: 'rgba(15, 23, 42, 0.6)', borderRadius: 10, padding: '12px 14px', marginBottom: 14, border: '1px solid rgba(255, 255, 255, 0.05)' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.68rem' }}>{isRtl ? 'کانال مبدأ (A)' : 'Source A'}</div>
                        <div style={{ fontWeight: 600, color: '#f8fafc', fontSize: '0.82rem', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {rule.source_chat_name || rule.source_chat_id}
                        </div>
                      </div>
                      <span className="font-mono-ltr" style={{ fontSize: '0.72rem', color: '#64748b', flexShrink: 0, marginInlineStart: 8 }}>
                        {rule.source_chat_id}
                      </span>
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, padding: '2px 0' }}>
                      <div style={{ flex: 1, height: 1, background: 'rgba(255,255,255,0.08)' }} />
                      <span className="badge badge-direct" style={{ fontSize: '0.68rem', padding: '2px 8px' }}>
                        <ArrowDown size={11} /> {rule.use_intermediate ? (isRtl ? 'واسط برند C' : 'Hop C') : (isRtl ? 'مستقیم' : 'Direct')}
                      </span>
                      <div style={{ flex: 1, height: 1, background: 'rgba(255,255,255,0.08)' }} />
                    </div>

                    {rule.use_intermediate && (
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', background: 'rgba(245, 158, 11, 0.08)', padding: '6px 10px', borderRadius: 6 }}>
                        <div style={{ minWidth: 0, flex: 1 }}>
                          <div style={{ color: '#fbbf24', fontSize: '0.68rem' }}>{isRtl ? 'کانال واسط (C)' : 'Hop C'}</div>
                          <div style={{ fontWeight: 600, color: '#fbbf24', fontSize: '0.8rem', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {rule.intermediate_channel_name || rule.intermediate_channel_id}
                          </div>
                        </div>
                        <span className="font-mono-ltr" style={{ fontSize: '0.7rem', color: '#b45309', flexShrink: 0, marginInlineStart: 8 }}>
                          {rule.intermediate_channel_id}
                        </span>
                      </div>
                    )}

                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.68rem' }}>{isRtl ? 'مقصد نهایی (B)' : 'Target B'}</div>
                        <div style={{ fontWeight: 600, color: '#f8fafc', fontSize: '0.82rem', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {rule.target_chat_name || rule.target_chat_id}
                        </div>
                      </div>
                      <span className="font-mono-ltr" style={{ fontSize: '0.72rem', color: '#64748b', flexShrink: 0, marginInlineStart: 8 }}>
                        {rule.target_chat_id}
                      </span>
                    </div>
                  </div>

                  {/* Channel Flow Diagram — Desktop (Horizontal Row) */}
                  <div
                    className="channel-flow-desktop"
                    style={{
                      background: 'rgba(15, 23, 42, 0.6)',
                      borderRadius: 10,
                      padding: '12px 14px',
                      marginBottom: 14,
                      border: '1px solid rgba(255, 255, 255, 0.05)',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      gap: 8,
                      fontSize: '0.82rem',
                    }}
                  >
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ color: '#94a3b8', fontSize: '0.7rem' }}>{isRtl ? 'کانال مبدا (A)' : 'Source A'}</div>
                      <div style={{ fontWeight: 600, color: '#f8fafc', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {rule.source_chat_name || rule.source_chat_id}
                      </div>
                      <div style={{ fontSize: '0.7rem', color: '#64748b' }}>
                        <span className="font-mono-ltr">{rule.source_chat_id}</span>
                      </div>
                    </div>

                    {rule.use_intermediate ? (
                      <>
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: '#fbbf24', flexShrink: 0 }}>
                          <span style={{ fontSize: '0.65rem' }}>Hop</span>
                          <ArrowRightLeft size={14} />
                        </div>
                        <div style={{ flex: 1, minWidth: 0, textAlign: 'center' }}>
                          <div style={{ color: '#fbbf24', fontSize: '0.7rem' }}>{isRtl ? 'واسط برند (C)' : 'Brand C'}</div>
                          <div style={{ fontWeight: 600, color: '#fbbf24', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {rule.intermediate_channel_name || rule.intermediate_channel_id || 'Channel C'}
                          </div>
                          <div style={{ fontSize: '0.7rem', color: '#b45309' }}>
                            <span className="font-mono-ltr">{rule.intermediate_channel_id}</span>
                          </div>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: '#10b981', flexShrink: 0 }}>
                          <span style={{ fontSize: '0.65rem' }}>Native</span>
                          {isRtl ? <ChevronLeft size={14} /> : <ChevronRight size={14} />}
                        </div>
                      </>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: '#3b82f6', padding: '0 8px', flexShrink: 0 }}>
                        <span style={{ fontSize: '0.65rem' }}>Direct</span>
                        {isRtl ? <ChevronLeft size={14} /> : <ChevronRight size={14} />}
                      </div>
                    )}

                    <div style={{ flex: 1, minWidth: 0, textAlign: isRtl ? 'left' : 'right' }}>
                      <div style={{ color: '#94a3b8', fontSize: '0.7rem' }}>{isRtl ? 'مقصد نهایی (B)' : 'Target B'}</div>
                      <div style={{ fontWeight: 600, color: '#f8fafc', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {rule.target_chat_name || rule.target_chat_id}
                      </div>
                      <div style={{ fontSize: '0.7rem', color: '#64748b' }}>
                        <span className="font-mono-ltr">{rule.target_chat_id}</span>
                      </div>
                    </div>
                  </div>

                  {/* Criteria & Details */}
                  <div style={{ fontSize: '0.78rem', color: '#cbd5e1', marginBottom: 12 }}>
                    {rule.custom_header && (
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                        <span style={{ color: '#94a3b8' }}>{isRtl ? 'هدر پیام:' : 'Header:'}</span>
                        <code style={{ background: 'rgba(255,255,255,0.06)', padding: '2px 6px', borderRadius: 4, color: '#fbbf24' }}>
                          {rule.custom_header}
                        </code>
                      </div>
                    )}
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
                      <span className="badge badge-gray" style={{ fontSize: '0.68rem', padding: '2px 6px' }}>
                        {rule.link_policy}
                      </span>
                      {rule.filter_rule_id && (
                        <span className="badge badge-direct" style={{ fontSize: '0.68rem', padding: '2px 6px' }}>
                          <Filter size={9} /> Filter
                        </span>
                      )}
                      {rule.ai_config_id && (
                        <span className="badge badge-vip" style={{ fontSize: '0.68rem', padding: '2px 6px' }}>
                          <Bot size={9} /> AI
                        </span>
                      )}
                      {rule.fallback_enabled && (
                        <span className="badge badge-gray" style={{ fontSize: '0.68rem', padding: '2px 6px', color: '#a7f3d0' }}>
                          <Shield size={9} /> Fallback: {rule.fallback_mode}
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
                      <div style={{ flex: 1, padding: '7px 6px', background: 'rgba(16, 185, 129, 0.10)', textAlign: 'center' }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.64rem', fontWeight: 600 }}>{isRtl ? 'فوروارد' : 'FWD'}</div>
                        <div style={{ fontWeight: 800, color: '#34d399', lineHeight: 1.35 }}>{liveStat.forwarded ?? 0}</div>
                      </div>
                      <div style={{ flex: 1, padding: '7px 6px', background: 'rgba(59, 130, 246, 0.10)', textAlign: 'center' }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.64rem', fontWeight: 600 }}>{isRtl ? 'فیلتر' : 'FILT'}</div>
                        <div style={{ fontWeight: 800, color: '#60a5fa', lineHeight: 1.35 }}>{liveStat.filtered ?? 0}</div>
                      </div>
                      <div style={{ flex: 1, padding: '7px 6px', background: liveStat.errors > 0 ? 'rgba(239, 68, 68, 0.18)' : 'rgba(239, 68, 68, 0.07)', textAlign: 'center' }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.64rem', fontWeight: 600 }}>{isRtl ? 'خطا' : 'ERR'}</div>
                        <div style={{ fontWeight: 800, color: liveStat.errors > 0 ? '#fca5a5' : '#64748b', lineHeight: 1.35 }}>{liveStat.errors ?? 0}</div>
                      </div>
                      <div style={{ flex: 1.3, padding: '7px 6px', background: 'rgba(99, 102, 241, 0.06)', textAlign: 'center' }}>
                        <div style={{ color: '#64748b', fontSize: '0.62rem' }}>{isRtl ? 'آخرین فوروارد' : 'LAST'}</div>
                        <div style={{ fontWeight: 700, color: '#818cf8', fontSize: '0.7rem' }}>
                          {liveStat.last_forward_ts
                            ? new Date(liveStat.last_forward_ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
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
                      paddingTop: 10,
                    }}
                  >
                    <div className="route-switch-grid">
                      <button
                        onClick={() => handleQuickRouteSwitch(rule.id, 'route1_direct')}
                        className={`route-switch-btn btn ${isDirectCopy ? 'btn-primary' : 'btn-secondary'}`}
                      >
                        <Copy size={11} />
                        <span>{isRtl ? 'مسیر ۱: مستقیم' : 'Route 1: Direct'}</span>
                      </button>

                      <button
                        onClick={() => handleQuickRouteSwitch(rule.id, 'route2_vip_hop')}
                        className={`route-switch-btn btn ${isVipHop ? 'btn-vip' : 'btn-secondary'}`}
                      >
                        <Sparkles size={11} />
                        <span>{isRtl ? 'مسیر ۲: VIP' : 'Route 2: VIP'}</span>
                      </button>

                      <button
                        onClick={() => handleQuickRouteSwitch(rule.id, 'route3_native')}
                        className={`route-switch-btn btn ${isNative ? 'btn-primary' : 'btn-secondary'}`}
                      >
                        <Send size={11} />
                        <span>{isRtl ? 'مسیر ۳: نیتیو' : 'Route 3: Native'}</span>
                      </button>
                    </div>
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

      {/* TAB: AI PV ASSISTANT */}
      {activeTab === 'pv' && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))', gap: 24 }}>
          {/* Main Controls & Prompt */}
          <div className="glass-panel" style={{ padding: 24 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <div>
                <h2 style={{ fontSize: '1.25rem', fontWeight: 800, display: 'flex', alignItems: 'center', gap: 10 }}>
                  <MessageSquare size={22} color="#ec4899" />
                  <span>{isRtl ? 'دستیار هوشمند پی‌وی (PV Assistant)' : 'AI PV Assistant'}</span>
                </h2>
                <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                  {isRtl
                    ? 'پاسخگویی خودکار، کاملاً انسانی، صمیمی و محاوره‌ای به پیام‌های خصوصی تلگرام'
                    : 'Human-like conversational auto-responder for Telegram private messages'}
                </p>
              </div>

              {/* Instant On/Off Toggle Button */}
              <button
                onClick={() => handleSavePV({ enabled: !pvConfig?.enabled })}
                disabled={pvSaving}
                className="btn"
                style={{
                  padding: '8px 16px',
                  background: pvConfig?.enabled ? 'rgba(16, 185, 129, 0.2)' : 'rgba(239, 68, 68, 0.2)',
                  color: pvConfig?.enabled ? '#34d399' : '#f87171',
                  border: pvConfig?.enabled ? '1px solid rgba(16, 185, 129, 0.4)' : '1px solid rgba(239, 68, 68, 0.4)',
                  fontWeight: 700,
                  fontSize: '0.85rem',
                  display: 'inline-flex',
                  alignItems: 'center',
                }}
              >
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: '50%',
                    background: pvConfig?.enabled ? '#10b981' : '#ef4444',
                    boxShadow: pvConfig?.enabled ? '0 0 8px #10b981' : 'none',
                    display: 'inline-block',
                    marginLeft: isRtl ? 8 : 0,
                    marginRight: isRtl ? 0 : 8,
                  }}
                />
                <span>{pvConfig?.enabled ? (isRtl ? 'فعال (روشن)' : 'ENABLED') : (isRtl ? 'غیرفعال (خاموش)' : 'DISABLED')}</span>
              </button>
            </div>

            {/* Persona Preset Buttons */}
            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 8, fontWeight: 600 }}>
                {isRtl ? 'لحن و پرسونا آماده:' : 'Persona & Tone Presets:'}
              </label>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 8 }}>
                <button
                  type="button"
                  onClick={() => {
                    setPvPreset('casual')
                    setPvConfig((prev) => prev ? { ...prev, persona_prompt: PV_PRESETS.casual.prompt } : null)
                  }}
                  className="btn"
                  style={{
                    background: pvPreset === 'casual' ? 'rgba(236, 72, 153, 0.2)' : 'rgba(255, 255, 255, 0.05)',
                    color: pvPreset === 'casual' ? '#f472b6' : '#cbd5e1',
                    border: pvPreset === 'casual' ? '1px solid rgba(236, 72, 153, 0.5)' : '1px solid rgba(255, 255, 255, 0.08)',
                    fontSize: '0.78rem',
                    padding: '8px 10px',
                    textAlign: 'center',
                  }}
                >
                  {isRtl ? PV_PRESETS.casual.label_fa : PV_PRESETS.casual.label_en}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setPvPreset('business')
                    setPvConfig((prev) => prev ? { ...prev, persona_prompt: PV_PRESETS.business.prompt } : null)
                  }}
                  className="btn"
                  style={{
                    background: pvPreset === 'business' ? 'rgba(59, 130, 246, 0.2)' : 'rgba(255, 255, 255, 0.05)',
                    color: pvPreset === 'business' ? '#60a5fa' : '#cbd5e1',
                    border: pvPreset === 'business' ? '1px solid rgba(59, 130, 246, 0.5)' : '1px solid rgba(255, 255, 255, 0.08)',
                    fontSize: '0.78rem',
                    padding: '8px 10px',
                    textAlign: 'center',
                  }}
                >
                  {isRtl ? PV_PRESETS.business.label_fa : PV_PRESETS.business.label_en}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setPvPreset('short')
                    setPvConfig((prev) => prev ? { ...prev, persona_prompt: PV_PRESETS.short.prompt } : null)
                  }}
                  className="btn"
                  style={{
                    background: pvPreset === 'short' ? 'rgba(245, 158, 11, 0.2)' : 'rgba(255, 255, 255, 0.05)',
                    color: pvPreset === 'short' ? '#fbbf24' : '#cbd5e1',
                    border: pvPreset === 'short' ? '1px solid rgba(245, 158, 11, 0.5)' : '1px solid rgba(255, 255, 255, 0.08)',
                    fontSize: '0.78rem',
                    padding: '8px 10px',
                    textAlign: 'center',
                  }}
                >
                  {isRtl ? PV_PRESETS.short.label_fa : PV_PRESETS.short.label_en}
                </button>
              </div>
            </div>

            {/* Persona Prompt Textarea */}
            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6, fontWeight: 600 }}>
                {isRtl ? 'دستورالعمل و پرامپت پرسونا (Persona Prompt):' : 'Persona Prompt Instructions:'}
              </label>
              <textarea
                rows={9}
                value={pvConfig?.persona_prompt || ''}
                onChange={(e) => {
                  setPvPreset('custom')
                  setPvConfig((prev) => prev ? { ...prev, persona_prompt: e.target.value } : null)
                }}
                className="input-field"
                style={{ width: '100%', fontFamily: 'inherit', fontSize: '0.825rem', lineHeight: 1.6 }}
                placeholder={isRtl ? 'دستورالعمل رفتار هوش مصنوعی در چت پی‌وی...' : 'AI behavior prompt for private chats...'}
              />
            </div>

            {/* Connect to AI Config */}
            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                {isRtl ? 'مدل هوش مصنوعی متصل:' : 'Linked AI Model Configuration:'}
              </label>
              <select
                value={pvConfig?.ai_config_id || ''}
                onChange={(e) => setPvConfig((prev) => prev ? { ...prev, ai_config_id: e.target.value || undefined } : null)}
                className="input-field"
                style={{ width: '100%' }}
              >
                <option value="">{isRtl ? '🤖 مدل پیش‌فرض سیستم (Default)' : '🤖 System Default'}</option>
                {aiConfigs.map((ai) => (
                  <option key={ai.id} value={ai.id}>
                    {ai.name} ({ai.provider} - {ai.model_name})
                  </option>
                ))}
              </select>
            </div>

            {/* Tuning Settings Grid */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 12, marginBottom: 18 }}>
              <div>
                <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {isRtl ? 'حداقل تایپینگ (ثانیه):' : 'Min Typing Delay (s):'}
                </label>
                <input
                  type="number"
                  step="0.5"
                  min="0.5"
                  max="10"
                  value={pvConfig?.typing_delay_min ?? 2.0}
                  onChange={(e) => setPvConfig((prev) => prev ? { ...prev, typing_delay_min: parseFloat(e.target.value) || 1 } : null)}
                  className="input-field"
                  style={{ width: '100%' }}
                />
              </div>
              <div>
                <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {isRtl ? 'حداکثر تایپینگ (ثانیه):' : 'Max Typing Delay (s):'}
                </label>
                <input
                  type="number"
                  step="0.5"
                  min="1"
                  max="15"
                  value={pvConfig?.typing_delay_max ?? 4.5}
                  onChange={(e) => setPvConfig((prev) => prev ? { ...prev, typing_delay_max: parseFloat(e.target.value) || 2 } : null)}
                  className="input-field"
                  style={{ width: '100%' }}
                />
              </div>
              <div>
                <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {isRtl ? 'کول‌داون پیام (ثانیه):' : 'Cooldown (s):'}
                </label>
                <input
                  type="number"
                  min="5"
                  max="120"
                  value={pvConfig?.cooldown_seconds ?? 15}
                  onChange={(e) => setPvConfig((prev) => prev ? { ...prev, cooldown_seconds: parseInt(e.target.value) || 10 } : null)}
                  className="input-field"
                  style={{ width: '100%' }}
                />
              </div>
              <div>
                <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {isRtl ? 'حافظه تاریخچه چت:' : 'Chat History Limit:'}
                </label>
                <input
                  type="number"
                  min="1"
                  max="20"
                  value={pvConfig?.history_limit ?? 4}
                  onChange={(e) => setPvConfig((prev) => prev ? { ...prev, history_limit: parseInt(e.target.value) || 4 } : null)}
                  className="input-field"
                  style={{ width: '100%' }}
                />
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: '0.8rem', color: '#cbd5e1' }}>
                <input
                  type="checkbox"
                  checked={pvConfig?.ignore_bots ?? true}
                  onChange={(e) => setPvConfig((prev) => prev ? { ...prev, ignore_bots: e.target.checked } : null)}
                  style={{ accentColor: '#ec4899' }}
                />
                <span>{isRtl ? '🤖 نادیده‌گرفتن پیام‌های ربات‌ها' : 'Ignore Bot Messages'}</span>
              </label>

              <button
                type="button"
                onClick={() => handleSavePV()}
                disabled={pvSaving}
                className="btn btn-primary"
                style={{ background: '#ec4899', borderColor: '#db2777' }}
              >
                <Check size={16} />
                <span>{pvSaving ? (isRtl ? 'در حال ذخیره...' : 'Saving...') : (isRtl ? 'ذخیره تنظیمات پی‌وی' : 'Save PV Config')}</span>
              </button>
            </div>
          </div>

          {/* Interactive Simulation / Testing Playground */}
          <div className="glass-panel" style={{ padding: 24, display: 'flex', flexDirection: 'column' }}>
            <h3 style={{ fontSize: '1.1rem', fontWeight: 700, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
              <Sparkles size={18} color="#f472b6" />
              <span>{isRtl ? 'تست زنده شبیه‌ساز چت پی‌وی' : 'Live PV Chat Simulator'}</span>
            </h3>
            <p style={{ fontSize: '0.8rem', color: '#94a3b8', marginBottom: 16 }}>
              {isRtl
                ? 'پیام تستی وارد کنید تا واکنش دستیار با شبیه‌سازی تاخیر تایپینگ انسانی نمایش داده شود.'
                : 'Enter a test message to see how the assistant replies with realistic typing delay.'}
            </p>

            <div style={{ marginBottom: 14 }}>
              <label style={{ display: 'block', fontSize: '0.78rem', color: '#cbd5e1', marginBottom: 4 }}>
                {isRtl ? 'پیام تستی مخاطب:' : 'Incoming Test Message:'}
              </label>
              <input
                type="text"
                value={pvTestText}
                onChange={(e) => setPvTestText(e.target.value)}
                className="input-field"
                style={{ width: '100%' }}
                placeholder={isRtl ? 'پیام خود را بنویسید...' : 'Type message...'}
              />
            </div>

            <button
              type="button"
              onClick={handleTestPVSimulate}
              disabled={pvTesting || !pvTestText.trim()}
              className="btn btn-secondary"
              style={{
                alignSelf: 'flex-start',
                marginBottom: 18,
                color: '#f472b6',
                border: '1px solid rgba(236, 72, 153, 0.4)',
              }}
            >
              <Send size={14} />
              <span>{pvTesting ? (isRtl ? '✍️ در حال تایپینگ...' : '✍️ Typing...') : (isRtl ? 'ارسال پیام تستی' : 'Send Test')}</span>
            </button>

            {/* Chat Simulation Bubble Box */}
            <div
              style={{
                flex: 1,
                minHeight: 180,
                background: 'rgba(15, 23, 42, 0.6)',
                borderRadius: 12,
                border: '1px solid rgba(255, 255, 255, 0.08)',
                padding: 16,
                display: 'flex',
                flexDirection: 'column',
                gap: 12,
                justifyContent: 'flex-end',
              }}
            >
              {/* User bubble */}
              <div style={{ alignSelf: isRtl ? 'flex-start' : 'flex-end', maxWidth: '80%' }}>
                <div
                  style={{
                    background: 'rgba(99, 102, 241, 0.25)',
                    border: '1px solid rgba(99, 102, 241, 0.4)',
                    color: '#e2e8f0',
                    padding: '8px 14px',
                    borderRadius: 12,
                    fontSize: '0.85rem',
                  }}
                >
                  {pvTestText}
                </div>
                <div style={{ fontSize: '0.7rem', color: '#64748b', marginTop: 2, textAlign: isRtl ? 'left' : 'right' }}>
                  {isRtl ? 'مخاطب' : 'Incoming'}
                </div>
              </div>

              {/* Typing indicator */}
              {pvTesting && (
                <div style={{ alignSelf: isRtl ? 'flex-end' : 'flex-start', maxWidth: '80%' }}>
                  <div
                    style={{
                      background: 'rgba(236, 72, 153, 0.15)',
                      border: '1px solid rgba(236, 72, 153, 0.3)',
                      color: '#f472b6',
                      padding: '8px 14px',
                      borderRadius: 12,
                      fontSize: '0.825rem',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                    }}
                  >
                    <span>✍️</span>
                    <span>{isRtl ? 'در حال تایپ پاسخ انسانی...' : 'Typing human-like reply...'}</span>
                  </div>
                </div>
              )}

              {/* Assistant reply bubble */}
              {pvTestReply && !pvTesting && (
                <div style={{ alignSelf: isRtl ? 'flex-end' : 'flex-start', maxWidth: '80%' }}>
                  <div
                    style={{
                      background: 'rgba(236, 72, 153, 0.2)',
                      border: '1px solid rgba(236, 72, 153, 0.45)',
                      color: '#fdf2f8',
                      padding: '10px 14px',
                      borderRadius: 12,
                      fontSize: '0.85rem',
                      lineHeight: 1.5,
                    }}
                  >
                    {pvTestReply}
                  </div>
                  <div style={{ fontSize: '0.7rem', color: '#f472b6', marginTop: 2, textAlign: isRtl ? 'right' : 'left' }}>
                    {isRtl ? 'دستیار پی‌وی (طبیعی و انسان‌نما)' : 'PV Assistant (Human Persona)'}
                  </div>
                </div>
              )}
            </div>
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

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 24 }}>
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
