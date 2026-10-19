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
  ListTodo,
  Download,
  Upload,
  ShieldAlert,
  Sliders,
  SlidersHorizontal,
} from 'lucide-react'
import { api } from './api'
import { isAccepted, purgedCount, refusalReason } from './actionResult'
import { describeKeyCheck, describeSaveFailure } from './providerKey'
import { SUPPORTED_LANGS, isRtlLang, makeT, preferredLang, rememberLang, type UiLang } from './locales'
import { AdminPanel } from './components/AdminPanel'
import { LanguagePicker } from './components/LanguagePicker'
import { AuditEventsPanel } from './components/AuditEventsPanel'
import { ClientSettings } from './components/ClientSettings'
import { TaskOverviewModal } from './components/TaskOverviewModal'
import { DiagnosticsPanel } from './components/DiagnosticsPanel'
import { JobsPanel } from './components/JobsPanel'
import { LogPolicyPanel } from './components/LogPolicyPanel'
import { SystemPanel } from './components/SystemPanel'
import type {
  ForwardRule,
  SaveRuleRequest,
  Session,
  AIConfig,
  AIProviderInfo,
  CheckProviderKeyResponse,
  SaveAIConfigRequest,
  PromptTemplate,
  TestPromptResponse,
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
  ServicesHealthResponse,
  PVResponderConfig,
  DiagnosticsResponse,
  MeInfo,
} from './types'
import { getToken, applySession, clearSession } from './session'

// ---------------------------------------------------------------------------
// Branding hop (A → C → B)
//
// C is one of the operator's own channels: a matched message is copied into C
// first and only then delivered from C to the final target B, so B's forward
// header names C instead of the original source channel.
//
// The enable switch and channel C are first-class rule fields
// (`use_intermediate` / `intermediate_channel_id`). The trigger configuration
// rides in the rule's free-form metadata under `branding_hop` — the same blob
// the bot, the per-task settings registry and the engine's decision read.
//
// Every key is optional on the wire and no configured trigger means "always
// hop", so the editor writes nothing until the operator actually changes a
// field: an untouched rule saves byte-identical metadata.
const BRANDING_HOP_KEY = 'branding_hop'

interface BrandingHopConfig {
  triggers_enabled: boolean
  origin_ids: string[]
  origin_titles: string[]
  keywords: string[]
  match_mode: 'any' | 'all'
  require_forward: boolean
  force_forward: boolean
}

const BRANDING_HOP_DEFAULTS: BrandingHopConfig = {
  triggers_enabled: false,
  origin_ids: [],
  origin_titles: [],
  keywords: [],
  match_mode: 'any',
  require_forward: false,
  force_forward: true,
}

/** Trimmed, de-duplicated, non-empty entries — the server coerces the same way. */
function brandingList(value: any): string[] {
  const items = Array.isArray(value) ? value : value === undefined || value === null || value === '' ? [] : [value]
  const out: string[] = []
  for (const item of items) {
    const text = String(item ?? '').trim()
    if (text && !out.includes(text)) out.push(text)
  }
  return out
}

/**
 * `metadata.branding_hop` as a complete config. A missing blob, a half-written
 * one, or a foreign type (another client may store anything under the key) all
 * fall back to the defaults instead of throwing while the editor renders.
 */
function readBrandingHop(meta: Record<string, any> | null | undefined): BrandingHopConfig {
  const raw = meta && typeof meta === 'object' ? meta[BRANDING_HOP_KEY] : null
  const blob: Record<string, any> = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
  return {
    triggers_enabled: blob.triggers_enabled === true,
    origin_ids: brandingList(blob.origin_ids),
    origin_titles: brandingList(blob.origin_titles),
    keywords: brandingList(blob.keywords),
    match_mode: String(blob.match_mode || '').toLowerCase() === 'all' ? 'all' : 'any',
    require_forward: blob.require_forward === true,
    force_forward: blob.force_forward === undefined ? BRANDING_HOP_DEFAULTS.force_forward : blob.force_forward === true,
  }
}

/** The list fields are edited as one comma-separated text input. */
function brandingListToText(value: string[] | undefined): string {
  return (value || []).join(', ')
}

function brandingTextToList(raw: string): string[] {
  return brandingList(String(raw || '').split(/[,\n]/))
}

/**
 * One plain-language line describing the route that will actually run, so the
 * operator reads the outcome instead of inferring it from a pile of switches.
 * Mirrors the engine's decision: no trigger means "always hop",
 * `require_forward` is a gate on top of the match, and `force_forward` is what
 * makes B show C's header (a copy from C would not).
 */
function brandingHopSummary(lang: UiLang, hop: BrandingHopConfig, route: string): string {
  const t = makeT(lang)
  const delivery = hop.force_forward
    ? t('ui_native_forward_from_c_b_shows_c')
    : t('ui_copy_from_c_no_branded_header')
  const gate = hop.triggers_enabled && hop.require_forward
    ? t('ui_only_if_the_message_is_itself_a_forward')
    : ''
  if (!hop.triggers_enabled) {
    return t('ui_hop_summary_every_message', { route, delivery })
  }
  const parts: string[] = []
  const origins = [...hop.origin_titles, ...hop.origin_ids]
  if (origins.length) {
    parts.push(t('ui_forwarded_from_origins', { origins: origins.join(' / ') }))
  }
  if (hop.keywords.length) {
    parts.push(t('ui_containing_keywords', { keywords: hop.keywords.join(' / ') }))
  }
  if (!parts.length) {
    return t('ui_hop_summary_every_message_gate', { route, delivery, gate })
  }
  const joiner = hop.match_mode === 'all' ? t('ui_and') : t('ui_or')
  return t('ui_hop_summary_only_matching', {
    route,
    scope: t('ui_only_messages'),
    parts: parts.join(joiner),
    delivery,
    gate,
  })
}

export function App({ me }: { me?: MeInfo } = {}) {
  // Language follows the user, not a hardcoded default: the stored choice
  // wins, then the browser's own language, then English. It used to default to
  // 'fa' with a two-way `en` branch, so every user's first dashboard render was
  // Persian and a pt/ru/zh browser had only two options.
  const [lang, setLang] = useState<UiLang>(() => preferredLang())

  // Persist the choice so the next visit — and the login page — opens in it.
  useEffect(() => {
    rememberLang(lang)
  }, [lang])
  const [activeTab, setActiveTab] = useState<'system' | 'rules' | 'simulator' | 'ai' | 'pv' | 'filters' | 'sessions' | 'jobs' | 'queue' | 'devops' | 'audit' | 'admin' | 'settings'>('system')
  // Identity starts from the session-core's verified answer when mounted with one.
  const [currentUser, setCurrentUser] = useState<{ user_id: number; username: string; is_admin: boolean } | null>(
    me ? { user_id: me.user_id, username: me.username, is_admin: me.is_admin } : null,
  )
  const [rules, setRules] = useState<ForwardRule[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [aiConfigs, setAiConfigs] = useState<AIConfig[]>([])
  // The provider catalogue comes from the core (one source of truth), never
  // from a table kept here: a provider offered in this select is one the
  // rewrite path can actually call.
  const [aiProviders, setAiProviders] = useState<AIProviderInfo[]>([])
  const [filterRules, setFilterRules] = useState<FilterRule[]>([])
  const [promptTemplates, setPromptTemplates] = useState<PromptTemplate[]>([])
  const [queueJobs, setQueueJobs] = useState<DeliveryJob[]>([])
  const [dlqJobs, setDlqJobs] = useState<DeadLetterJob[]>([])
  const [deliveryStats, setDeliveryStats] = useState<DeliveryStats | null>(null)
  const [ruleLiveStats, setRuleLiveStats] = useState<RuleLiveStat[]>([])
  const [recentErrors, setRecentErrors] = useState<RecentError[]>([])
  const [diagnostics, setDiagnostics] = useState<DiagnosticsResponse | null>(null)
  // Pending jobs badge for the "My Jobs" tab (from the panel's poll).
  const [jobsPending, setJobsPending] = useState(0)
  const [diagLoading, setDiagLoading] = useState(false)

  const refreshDiagnostics = async () => {
    setDiagLoading(true)
    try {
      setDiagnostics(await api.diagnostics())
    } catch {
      setDiagnostics(null)
    } finally {
      setDiagLoading(false)
    }
  }
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
  // v18 history controls: which task, which way, and how far back.
  const [logFilterRule, setLogFilterRule] = useState<string>('all')
  const [logOrder, setLogOrder] = useState<'desc' | 'asc'>('desc')
  const [logPageSize, setLogPageSize] = useState<number>(100)
  const [logOffset, setLogOffset] = useState<number>(0)
  const [logTotal, setLogTotal] = useState<number>(0)
  const [logHasMore, setLogHasMore] = useState<boolean>(false)
  const [logExporting, setLogExporting] = useState<boolean>(false)
  const [purging, setPurging] = useState(false)
  const [purgeWindowH, setPurgeWindowH] = useState<number>(24)
  const [purgeResult, setPurgeResult] = useState<{ purged: number; remaining: number } | null>(null)

  // ---- Backup & Restore: move this install (or one tenant) between servers
  const [backupBusy, setBackupBusy] = useState(false)
  const [backupPassphrase, setBackupPassphrase] = useState('')
  const [backupCounts, setBackupCounts] = useState<Record<string, number> | null>(null)
  const [backupEncrypted, setBackupEncrypted] = useState(false)
  const [importText, setImportText] = useState('')
  const [importPassphrase, setImportPassphrase] = useState('')
  const [importOverwrite, setImportOverwrite] = useState(false)
  const [importStats, setImportStats] = useState<Record<string, { imported: number; skipped: number; failed: number }> | null>(null)
  const [copiedLogId, setCopiedLogId] = useState<string | number | null>(null)
  const [servicesHealth, setServicesHealth] = useState<ServicesHealthResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [toast, setToast] = useState<string | null>(null)

  // Rule Modal
  const [isRuleModalOpen, setIsRuleModalOpen] = useState(false)
  const [ruleFormData, setRuleFormData] = useState<Partial<SaveRuleRequest>>({})
  // Bot-parity toggles that live inside rule metadata (block_voice, album_mode, …)
  const [ruleMeta, setRuleMeta] = useState<Record<string, any>>({})
  const [taskOverviewRule, setTaskOverviewRule] = useState<ForwardRule | null>(null)
  const [isTaskOverviewOpen, setIsTaskOverviewOpen] = useState(false)

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

  const isRtl = isRtlLang(lang)
  const t = makeT(lang)

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 4000)
  }

  const loadData = async () => {
    setLoading(true)
    try {
      const [r, s, ai, pt, f, q, dlq, gw, st, l, ds, pv, prov] = await Promise.all([
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
        api.getAIProviders().catch(() => []),
      ])
      setRules(Array.isArray(r) ? r : [])
      setSessions(Array.isArray(s) ? s : [])
      setAiConfigs(Array.isArray(ai) ? ai : [])
      setAiProviders(Array.isArray(prov) ? prov : [])
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

  /**
   * v18 log fetch: one page of history with the real match count. `offset`
   * walks backwards, so the console can reach the oldest record instead of
   * only ever showing the newest 100 rows.
   */
  const fetchLogs = async (
    srv = logFilterService,
    lvl = logFilterLevel,
    srch = logSearch,
    opts?: { rule?: string; order?: 'desc' | 'asc'; offset?: number; limit?: number },
  ) => {
    const rule = opts?.rule ?? logFilterRule
    const order = opts?.order ?? logOrder
    const offset = opts?.offset ?? logOffset
    const limit = opts?.limit ?? logPageSize
    try {
      const params: any = { limit, offset, ascending: order === 'asc' }
      if (srv !== 'all') params.service = srv
      if (lvl !== 'all') params.level = lvl
      if (srch.trim()) params.search = srch.trim()
      if (rule !== 'all') params.rule_id = rule
      const [page, stats] = await Promise.all([
        api.getLogsPage(params).catch(() => null),
        api.getLogStats(rule !== 'all' ? { rule_id: rule } : undefined).catch(() => null),
      ])
      setLogs(Array.isArray(page?.logs) ? page!.logs : [])
      setLogTotal(page?.total ?? 0)
      setLogHasMore(!!page?.has_more)
      if (stats) setLogStats(stats)
    } catch {}
  }

  const handleExportLogs = async (format: 'json' | 'csv') => {
    if (logExporting) return
    setLogExporting(true)
    try {
      await api.exportLogs({
        service: logFilterService !== 'all' ? logFilterService : undefined,
        level: logFilterLevel !== 'all' ? logFilterLevel : undefined,
        search: logSearch.trim() || undefined,
        rule_id: logFilterRule !== 'all' ? logFilterRule : undefined,
        format,
      })
      showToast(t('ui_log_export_downloaded'))
    } catch (e: any) {
      showToast(e?.message || (t('ui_export_failed')))
    } finally {
      setLogExporting(false)
    }
  }

  const handlePurgeLogs = async () => {
    if (purging) return
    const keepH = Math.max(0, purgeWindowH)
    // The log archive is one shared store and the purge path has no tenant
    // dimension, so an installation-wide purge is administrator-only: the API
    // refuses it with `rule_scope_required`. A tenant purges one of their own
    // tasks, which is also the only scope the log explorer can show them.
    // Asking for the refusal anyway would just look like a broken button.
    const purgeRuleId = logFilterRule !== 'all' ? logFilterRule : undefined
    if (!currentUser?.is_admin && !purgeRuleId) {
      showToast(t('ui_select_a_task_before_purging_its_logs'))
      return
    }
    const ageWindow = keepH > 0 ? t('ui_older_than_hours', { hours: keepH }) : t('ui_all_logs_no_time_bound')
    // The confirm names what is about to be deleted, filter by filter.
    const scope = [
      logFilterRule !== 'all' ? t('ui_task_scope_prefix', { task: logFilterRule.slice(0, 8) }) : '',
      logFilterLevel !== 'all' ? t('ui_level_scope_prefix', { level: logFilterLevel }) : '',
      logFilterService !== 'all' ? t('ui_service_scope_prefix', { service: logFilterService }) : '',
    ]
      .filter(Boolean)
      .join(' · ')
    if (!window.confirm(t('ui_confirm_purge_logs', { scope: scope ? `${scope} · ` : '', window: ageWindow }))) return
    setPurging(true)
    setPurgeResult(null)
    try {
      const res = await api.purgeLogs({
        level: logFilterLevel !== 'all' ? logFilterLevel : undefined,
        service: logFilterService !== 'all' ? logFilterService : undefined,
        rule_id: purgeRuleId,
        older_than_hours: keepH,
      })
      if (res?.success) {
        setPurgeResult({ purged: res.purged, remaining: res.remaining })
        showToast(t('ui_logs_purged', { purged: res.purged, remaining: res.remaining }))
        await fetchLogs()
      } else {
        showToast(res?.error || t('ui_error_purging_logs'))
      }
    } catch (e: any) {
      showToast(e.message || t('ui_error_purging_logs'))
    } finally {
      setPurging(false)
    }
  }

  // Produce a migration bundle of everything this account owns. The bundle
  // contains live credentials; a passphrase encrypts it with AES-GCM.
  const handleBackupExport = async () => {
    if (backupBusy) return
    if (!backupPassphrase && !window.confirm(t('ui_confirm_backup_without_passphrase'))) return
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
        const rows = Object.values(res.counts || {}).reduce((a, b) => a + b, 0)
        showToast(
          t('ui_backup_created', {
            rows,
            encryption: res.encrypted ? t('ui_backup_encrypted_suffix') : '',
          }),
        )
      } else {
        showToast(res?.error || t('ui_error_creating_backup'))
      }
    } catch (e: any) {
      showToast(e.message || t('ui_error_creating_backup'))
    } finally {
      setBackupBusy(false)
    }
  }

  // Apply a bundle from this or another server. Destructive when overwrite is on.
  const handleBackupImport = async () => {
    if (backupBusy) return
    const bundle = importText.trim()
    if (!bundle) {
      showToast(t('ui_backup_contents_required'))
      return
    }
    const looksEncrypted = bundle.startsWith('enc:')
    if (looksEncrypted && !importPassphrase) {
      showToast(t('ui_backup_passphrase_required'))
      return
    }
    const totalRows = importStats ? Object.values(importStats).reduce((s, v) => s + v.imported + v.skipped + v.failed, 0) : 0
    const verb = importOverwrite ? t('ui_replace_all') : t('ui_merge')
    const rowsNote = totalRows ? t('ui_import_rows_note', { rows: totalRows }) : ''
    if (!window.confirm(t('ui_confirm_backup_restore', { verb, rows: rowsNote }))) return
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
        showToast(
          t('ui_restore_complete', {
            rows: imported,
            failed: failed ? t('ui_restore_failed_note', { count: failed }) : '',
          }),
        )
        // everything the UI shows may have changed underneath it
        await loadData()
      } else {
        showToast(res?.error || t('ui_error_restoring_backup'))
      }
    } catch (e: any) {
      showToast(e.message || t('ui_error_restoring_backup'))
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

  useEffect(() => {
    // Telegram Mini App (TMA) auto-detection, viewport expansion & token exchange
    const tg = (window as any).Telegram?.WebApp
    if (tg) {
      try {
        tg.ready?.()
        tg.expand?.()
      } catch {}
      if (tg.initData && !getToken()) {
        api.authTMA(tg.initData).then((auth: any) => {
          if (auth && auth.token) {
            applySession(auth.token)
            api.me().then((info) => {
              setCurrentUser(info)
            }).catch(() => {})
            loadData()
            showToast(t('ui_telegram_mini_app_auto_authenticated'))
          }
        }).catch(() => {})
      }
    }

    loadData()
    refreshDiagnostics()
    api.me().then((info) => {
      setCurrentUser(info)
    }).catch(() => {})
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
    // Service-topology health: probe all 4 services through the API layer.
    api.getServicesHealth().then(setServicesHealth).catch(() => setServicesHealth(null))
    if (!logAutoRefresh) return
    const logTimer = setInterval(() => {
      fetchLogs(logFilterService, logFilterLevel, logSearch)
    }, 3000)
    const healthTimer = setInterval(() => {
      api.getServicesHealth().then(setServicesHealth).catch(() => setServicesHealth(null))
    }, 10000)
    return () => { clearInterval(logTimer); clearInterval(healthTimer) }
    // v18 history controls are part of the query, so the poller must re-arm
    // when they change — otherwise auto-refresh would silently re-fetch a
    // stale offset and the operator would watch a page they had navigated away
    // from.
  }, [activeTab, logAutoRefresh, logFilterService, logFilterLevel, logSearch, logFilterRule, logOrder, logOffset, logPageSize])

  // Rules Handlers
  const handleToggleRule = async (id: string) => {
    try {
      const updated = await api.toggleRule(id)
      setRules((prev) => prev.map((r) => (r.id === id ? updated : r)))
      showToast(t('ui_rule_status_updated'))
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleDeleteRule = async (id: string) => {
    if (!confirm(t('ui_are_you_sure_you_want_to_delete_this_rule'))) return
    try {
      await api.deleteRule(id)
      setRules((prev) => prev.filter((r) => r.id !== id))
      setIsTaskOverviewOpen(false)
      showToast(t('ui_rule_deleted_successfully'))
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleDuplicateRule = async (id: string) => {
    try {
      const cloned = await api.duplicateRule(id)
      setRules((prev) => [cloned, ...prev])
      showToast(t('ui_task_duplicated_successfully'))
    } catch (e: any) {
      showToast(e.message || (t('ui_failed_to_duplicate_task')))
    }
  }

  const handleSaveTaskOverview = async (updated: ForwardRule) => {
    try {
      const res = await api.saveRule(updated as any)
      setRules((prev) => prev.map((r) => (r.id === res.id ? res : r)))
      showToast(t('ui_task_updated_successfully'))
    } catch (e: any) {
      showToast(e.message || (t('ui_failed_to_save_task')))
    }
  }

  const handleQuickRouteSwitch = async (ruleId: string, pathType: 'route1_direct' | 'route2_vip_hop' | 'route3_native') => {
    try {
      const updated = await api.quickSetRoutePath(ruleId, {
        path_type: pathType,
      })
      setRules((prev) => prev.map((r) => (r.id === ruleId ? updated : r)))
      showToast(t('ui_rule_route_switched_successfully'))
    } catch (e: any) {
      showToast(e.message)
    }
  }

  // Branding hop (A → C → B) triggers: read from `metadata.branding_hop` and
  // merged back into the *raw* blob, so a sibling key written by the bot or
  // another client survives an edit here. Nothing is written until a field is
  // touched, which keeps an untouched rule's metadata byte-identical.
  const brandingHop = readBrandingHop(ruleMeta)
  const patchBrandingHop = (patch: Partial<BrandingHopConfig>) => {
    setRuleMeta((prev) => {
      const raw = prev?.[BRANDING_HOP_KEY]
      const blob = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
      return { ...prev, [BRANDING_HOP_KEY]: { ...blob, ...patch } }
    })
  }
  // The list fields are typed as free text, so a draft keeps exactly what was
  // typed: deriving the input value from the parsed list would eat the ", "
  // separator mid-keystroke and make a second entry impossible to type. The
  // draft is keyed by the open task, so switching tasks drops it during render
  // (no effect, no stale flash).
  const [brandingListDrafts, setBrandingListDrafts] = useState<{ key: string; values: Record<string, string> }>({
    key: '',
    values: {},
  })
  const brandingDraftKey = `${ruleFormData.id || 'new'}:${isRuleModalOpen ? 'open' : 'closed'}`
  const brandingListValue = (key: 'origin_ids' | 'origin_titles' | 'keywords') =>
    (brandingListDrafts.key === brandingDraftKey && brandingListDrafts.values[key] !== undefined)
      ? brandingListDrafts.values[key]
      : brandingListToText(brandingHop[key])
  const onBrandingListInput = (key: 'origin_ids' | 'origin_titles' | 'keywords', raw: string) => {
    setBrandingListDrafts((prev) => ({
      key: brandingDraftKey,
      values: { ...(prev.key === brandingDraftKey ? prev.values : {}), [key]: raw },
    }))
    patchBrandingHop({ [key]: brandingTextToList(raw) } as Partial<BrandingHopConfig>)
  }

  const handleSavePV = async (updated?: Partial<PVResponderConfig>) => {
    setPvSaving(true)
    try {
      const cfg: PVResponderConfig = {
        enabled: pvConfig?.enabled ?? false,
        persona_prompt: pvConfig?.persona_prompt || t('ui_pv_preset_casual_prompt'),
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
      showToast(t('ui_pv_assistant_config_saved_successfully'))
    } catch (e: any) {
      showToast(e.message || 'Error saving PV Assistant config')
    } finally {
      setPvSaving(false)
    }
  }

  const handleSaveRule = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      if (!ruleFormData.source_chat_id || !ruleFormData.target_chat_id) {
        showToast(t('ui_source_and_target_ids_are_required'))
        return
      }
      const s = (ruleFormData.source_chat_id || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
      const targetId = (ruleFormData.target_chat_id || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
      if (s === targetId) {
        showToast(t('ui_routing_loop_detected_source_and_target_ids_cannot_be_identical'))
        return
      }
      if (ruleFormData.use_intermediate && ruleFormData.intermediate_channel_id) {
        const im = (ruleFormData.intermediate_channel_id || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
        if (im === s || im === targetId) {
          showToast(t('ui_routing_loop_detected_intermediate_channel_cannot_match_source_o'))
          return
        }
      }
      const metaToSave: Record<string, any> = { ...ruleMeta }
      if (ruleFormData.ai_config_id) {
        metaToSave.ai_config_id = ruleFormData.ai_config_id
      }
      if (ruleMeta.prompt_id) {
        metaToSave.prompt_id = ruleMeta.prompt_id
      }
      const saved = await api.saveRule({
        ...ruleFormData,
        prompt_id: ruleMeta.prompt_id || ruleFormData.prompt_id,
        // The per-task metadata is sent through the top-level `metadata` object
        // as well as the JSON string. The server merges `metadata` *over*
        // `custom_metadata_json`, and editing a task spreads the rule as it came
        // back from a GET — so without this the stale copy wins and every edit
        // to a metadata-backed field (the branding-hop triggers included) is
        // silently reverted on save. Both objects carry the whole key set
        // (`metaToSave` is the metadata blob with this form's edits applied), so
        // no other feature loses a key.
        metadata: metaToSave,
        custom_metadata_json: JSON.stringify(metaToSave),
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
      showToast(t('ui_rule_saved_successfully'))
    } catch (e: any) {
      let msg = e.message || 'Error'
      if (msg.includes('loop_detected') || msg.includes('Invalid rule endpoints')) {
        msg = t('ui_routing_loop_detected_source_and_target_endpoints_cannot_be_iden')
      }
      if (msg.includes('not_numeric') || msg.includes('resolve_failed')) {
        msg = t('ui_invalid_chat_id_use_a_numeric_id_or_a_username_the_connected_acc')
      }
      showToast(msg)
    }
  }

  // AI Handlers
  const handleSaveAI = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      if (!aiFormData.name || !aiFormData.provider || !aiFormData.model) {
        showToast(t('ui_name_provider_and_model_are_required'))
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
      showToast(t('ui_ai_config_saved_successfully'))
    } catch (e: any) {
      // A refused endpoint arrives as its code; say what it means.
      showToast(describeSaveFailure(e?.message || '', lang))
    }
  }

  const handleDeleteAI = async (id: string) => {
    if (!confirm(t('ui_delete_this_ai_config'))) return
    try {
      await api.deleteAIConfig(id)
      setAiConfigs((prev) => prev.filter((a) => a.id !== id))
      showToast(t('ui_ai_config_deleted'))
    } catch (e: any) {
      showToast(e.message)
    }
  }

  // Filter Handlers
  const handleSaveFilter = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      if (!filterFormData.name) {
        showToast(t('ui_filter_name_is_required'))
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
      showToast(t('ui_filter_rule_saved_successfully'))
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleDeleteFilter = async (id: string) => {
    if (!confirm(t('ui_delete_this_filter_rule'))) return
    try {
      await api.deleteFilter(id)
      setFilterRules((prev) => prev.filter((f) => f.id !== id))
      showToast(t('ui_filter_rule_deleted'))
    } catch (e: any) {
      showToast(e.message)
    }
  }

  // DLQ Handlers
  const handleRetryDLQ = async (id: string) => {
    try {
      const resp: any = await api.retryDLQ(id)
      // Only an explicit `true` is a replay. A refusal — `not_found`,
      // `already_pending`, `already_sent`, `replay_failed` — must keep the row
      // and say why; testing `success === false` would treat a response that
      // omits the field as a replay that happened. The decision lives in
      // `actionResult.ts`, which the CI job compiles and asserts on.
      if (!isAccepted(resp)) {
        showToast(`${t('ui_retry_refused')} (${refusalReason(resp)})`)
        await loadData()
        return
      }
      setDlqJobs((prev) => prev.filter((j) => j.id !== id))
      showToast(t('ui_job_requeued_for_retry'))
      await loadData()
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handlePurgeDLQ = async () => {
    if (!confirm(t('ui_purge_all_dlq_entries'))) return
    try {
      const resp: any = await api.purgeDLQ()
      if (!isAccepted(resp)) {
        showToast(`${t('ui_purge_failed')} (${refusalReason(resp, 'purge_failed')})`)
        return
      }
      setDlqJobs([])
      // The core answers with the number it removed; showing that number is what
      // distinguishes a real purge from a purge that matched nothing.
      const count = purgedCount(resp)
      showToast(
        count !== null
          ? (t('ui_dlq_purged_count_2', { count_2: count }))
          : (t('ui_dlq_purged')),
      )
      await loadData()
    } catch (e: any) {
      showToast(e.message)
    }
  }

  // Rule pause / resume (mirrors bot's pause/resume feature)
  const handlePauseRule = async (id: string) => {
    try {
      await api.pauseRule(id)
      setRules((prev) => prev.map((r) => (r.id === id ? { ...r, is_paused: true } : r)))
      showToast(t('ui_rule_paused'))
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleResumeRule = async (id: string) => {
    try {
      await api.resumeRule(id)
      setRules((prev) => prev.map((r) => (r.id === id ? { ...r, is_paused: false } : r)))
      showToast(t('ui_rule_resumed'))
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
        showToast(t('ui_session_backup_downloaded'))
      } else {
        showToast(res.message || (t('ui_backup_failed')))
      }
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleTerminateSession = async (sess: Session) => {
    if (!confirm(t('ui_terminate_session_sess_2', { sess_2: sess.first_name || sess.username || sess.id }))) return
    try {
      const res = await api.terminateSession(sess.id)
      if (res.success) {
        setSessions((prev) => prev.filter((s) => s.id !== sess.id))
        showToast(t('ui_session_terminated'))
      } else {
        showToast(res.message || (t('ui_terminate_failed')))
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
            title={t('ui_dismiss')}
          >
            <X size={14} />
          </button>
        </div>
      )}
      {/* Toast Notification */}
      {toast && (
        <div
          className="toast-fixed"
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
        <div className="header-title-wrap" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
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
              {t('ui_unified_smart_telegram_forwarding_gateway_microservice_platform')}
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

          {/* Language picker: all ten catalogs, by endonym */}
          <LanguagePicker lang={lang} onChange={setLang} compact />

          {/* Reload Button */}
          <button
            onClick={loadData}
            disabled={loading}
            className="btn btn-secondary"
            style={{ padding: '5px 10px', fontSize: '0.78rem' }}
            title={t('ui_refresh')}
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
            <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>{t('ui_active_rules')}</div>
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
            <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>{t('ui_forwarded_24h')}</div>
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
            <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>{t('ui_connected_sessions')}</div>
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
            <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>{t('ui_dead_letter_queue')}</div>
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
          onClick={() => setActiveTab('system')}
          className="btn"
          style={{
            background: activeTab === 'system' ? 'rgba(56, 189, 248, 0.2)' : 'transparent',
            color: activeTab === 'system' ? '#38bdf8' : '#94a3b8',
            border: activeTab === 'system' ? '1px solid rgba(56, 189, 248, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'system' ? 700 : 500,
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <Server size={15} />
          <span>{t('ui_system')}</span>
        </button>

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
          <span>{t('ui_rules_routes')}</span>
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
          <span>{t('ui_smart_simulator')}</span>
          <span className="badge badge-vip" style={{ fontSize: '0.65rem', padding: '1px 6px' }}>{t('ui_env_test')}</span>
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
          <span>{t('ui_ai_intelligence')}</span>
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
          <span>{t('ui_pv_assistant')}</span>
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
          <span>{t('ui_advanced_filters')}</span>
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
          <span>{t('ui_telegram_sessions')}</span>
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
          <span>{t('ui_queue_dlq')}</span>
          {dlqJobs.length > 0 && (
            <span style={{ background: '#ef4444', color: '#fff', borderRadius: '50%', padding: '1px 6px', fontSize: '0.7rem' }}>
              {dlqJobs.length}
            </span>
          )}
        </button>

        <button
          onClick={() => setActiveTab('jobs')}
          className="btn"
          style={{
            background: activeTab === 'jobs' ? 'rgba(20, 184, 166, 0.2)' : 'transparent',
            color: activeTab === 'jobs' ? '#2dd4bf' : '#94a3b8',
            border: '1px solid rgba(20, 184, 166, 0.4)',
            fontWeight: activeTab === 'jobs' ? 700 : 500,
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <ListTodo size={15} />
          <span>{t('ui_my_jobs')}</span>
          {jobsPending > 0 && (
            <span style={{ background: '#14b8a6', color: '#fff', borderRadius: '50%', padding: '1px 6px', fontSize: '0.7rem' }}>
              {jobsPending}
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
          <span>{t('ui_devops_logs')}</span>
        </button>

        <button
          onClick={() => setActiveTab('audit')}
          className="btn"
          style={{
            background: activeTab === 'audit' ? 'rgba(34, 197, 94, 0.2)' : 'transparent',
            color: activeTab === 'audit' ? '#4ade80' : '#94a3b8',
            border: activeTab === 'audit' ? '1px solid rgba(34, 197, 94, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'audit' ? 700 : 500,
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <Activity size={15} />
          <span>{t('ui_audit_events')}</span>
        </button>

        <button
          onClick={() => setActiveTab('settings')}
          className="btn"
          style={{
            background: activeTab === 'settings' ? 'rgba(99, 102, 241, 0.2)' : 'transparent',
            color: activeTab === 'settings' ? '#818cf8' : '#94a3b8',
            border: activeTab === 'settings' ? '1px solid rgba(99, 102, 241, 0.4)' : '1px solid transparent',
            fontWeight: activeTab === 'settings' ? 700 : 500,
            flexShrink: 0,
            whiteSpace: 'nowrap',
          }}
        >
          <Sliders size={15} />
          <span>{t('ui_client_settings')}</span>
        </button>

        {currentUser?.is_admin && (
          <button
            onClick={() => setActiveTab('admin')}
            className="btn"
            style={{
              background: activeTab === 'admin' ? 'rgba(239, 68, 68, 0.25)' : 'rgba(239, 68, 68, 0.1)',
              color: activeTab === 'admin' ? '#f87171' : '#fca5a5',
              border: activeTab === 'admin' ? '1px solid rgba(239, 68, 68, 0.5)' : '1px solid rgba(239, 68, 68, 0.25)',
              fontWeight: 700,
              flexShrink: 0,
              whiteSpace: 'nowrap',
            }}
          >
            <ShieldAlert size={15} />
            <span>{t('ui_master_admin')}</span>
            <span className="badge badge-vip" style={{ fontSize: '0.65rem', padding: '1px 6px', background: '#ef4444', color: '#fff' }}>{t('ui_role_admin')}</span>
          </button>
        )}
      </div>

      {/* TAB 0: SYSTEM — readiness, topology, contract */}
      {activeTab === 'system' && <SystemPanel lang={lang} />}

      {/* TAB 1: RULES & ROUTES */}
      {activeTab === 'rules' && (
        <div>
          <DiagnosticsPanel
            report={diagnostics}
            loading={diagLoading}
            onRefresh={refreshDiagnostics}
            lang={lang}
          />
          <div className="hero-container" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
            <div>
              <h2 style={{ fontSize: '1.2rem', fontWeight: 800, margin: 0 }}>
                {t('ui_smart_forwarding_routing_rules')}
              </h2>
              <p className="text-balance" style={{ fontSize: '0.82rem', color: '#94a3b8', marginTop: 4, margin: 0 }}>
                {t('ui_configure_triple_routes_branding_hops_bypass_policies_and_channe')}
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
              <span>{t('ui_new_rule')}</span>
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
                  <div className="rule-card-head" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12, gap: 8, flexWrap: 'wrap' }}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                      <div className="rule-badges" style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                        {isVipHop && (
                          <span className="badge badge-vip" style={{ fontSize: '0.72rem', padding: '3px 8px' }}>
                            <Sparkles size={11} />
                            {t('ui_route_2_vip_hop')}
                          </span>
                        )}
                        {isDirectCopy && (
                          <span className="badge badge-direct" style={{ fontSize: '0.72rem', padding: '3px 8px' }}>
                            <Copy size={11} />
                            {t('ui_route_1_direct_copy')}
                          </span>
                        )}
                        {isNative && (
                          <span className="badge badge-native" style={{ fontSize: '0.72rem', padding: '3px 8px' }}>
                            <Send size={11} />
                            {t('ui_route_3_native')}
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
                            {t('ui_loop')}
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

                    <div className="rule-card-actions" style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
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
                        {rule.is_active ? (t('ui_active')) : (t('ui_paused'))}
                      </button>

                      {rule.is_paused ? (
                        <button
                          onClick={() => handleResumeRule(rule.id)}
                          className="btn btn-secondary icon-btn"
                          style={{ padding: '4px 8px', fontSize: '0.72rem', color: '#34d399' }}
                          title={t('ui_resume')}
                        >
                          <PlayCircle size={12} />
                          <span>{t('ui_resume')}</span>
                        </button>
                      ) : (
                        <button
                          onClick={() => handlePauseRule(rule.id)}
                          className="btn btn-secondary icon-btn"
                          style={{ padding: '4px 8px', fontSize: '0.72rem', color: '#fbbf24' }}
                          title={t('ui_pause')}
                        >
                          <Clock size={12} />
                          <span>{t('ui_pause')}</span>
                        </button>
                      )}

                      <button
                        onClick={() => {
                          setTaskOverviewRule(rule)
                          setIsTaskOverviewOpen(true)
                        }}
                        className="btn btn-secondary icon-btn"
                        style={{ padding: '5px', background: 'rgba(59, 130, 246, 0.2)', color: '#60a5fa', borderColor: 'rgba(59, 130, 246, 0.4)' }}
                        title={t('ui_task_overview_advanced_settings')}
                      >
                        <SlidersHorizontal size={13} />
                      </button>

                      <button
                        onClick={() => {
                          setRuleFormData({ ...rule })
                          try {
                            const parsed = JSON.parse((rule as any).custom_metadata_json || '{}')
                            if ((rule as any).prompt_id && !parsed.prompt_id) {
                              parsed.prompt_id = (rule as any).prompt_id
                            }
                            setRuleMeta(parsed)
                          } catch {
                            setRuleMeta((rule as any).prompt_id ? { prompt_id: (rule as any).prompt_id } : {})
                          }
                          setIsRuleModalOpen(true)
                        }}
                        className="btn btn-secondary icon-btn"
                        style={{ padding: '5px' }}
                        title={t('ui_edit')}
                      >
                        <Edit3 size={13} />
                      </button>

                      <button
                        onClick={() => handleDuplicateRule(rule.id)}
                        className="btn btn-secondary icon-btn"
                        style={{ padding: '5px' }}
                        title={t('ui_duplicate_task')}
                      >
                        <Copy size={13} />
                      </button>

                      <button
                        onClick={() => handleDeleteRule(rule.id)}
                        className="btn btn-danger icon-btn"
                        style={{ padding: '5px' }}
                        title={t('ui_delete')}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </div>

                  {/* Channel Flow Diagram — Mobile (Vertical Stack) */}
                  <div className="channel-flow-mobile" style={{ background: 'rgba(15, 23, 42, 0.6)', borderRadius: 10, padding: '12px 14px', marginBottom: 14, border: '1px solid rgba(255, 255, 255, 0.05)', overflow: 'hidden' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, minWidth: 0 }}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.68rem' }}>
                          {t('ui_source_a')}
                          {extraSourceCount(rule) > 0 && (
                            <span className="badge" style={{ fontSize: '0.62rem', marginInlineStart: 6, background: 'rgba(59, 130, 246, 0.18)', color: '#93c5fd' }}>
                              +{extraSourceCount(rule)}
                            </span>
                          )}
                        </div>
                        <div style={{ fontWeight: 600, color: '#f8fafc', fontSize: '0.82rem', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {rule.source_chat_name || rule.source_chat_id}
                        </div>
                      </div>
                      <span className="font-mono-ltr channel-id-mono" style={{ fontSize: '0.72rem', color: '#64748b', flexShrink: 0, marginInlineStart: 8, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '46%' }}>
                        {rule.source_chat_id}
                      </span>
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, padding: '2px 0' }}>
                      <div style={{ flex: 1, height: 1, background: 'rgba(255,255,255,0.08)' }} />
                      <span className="badge badge-direct" style={{ fontSize: '0.68rem', padding: '2px 8px' }}>
                        <ArrowDown size={11} /> {rule.use_intermediate ? (t('ui_hop_c')) : (t('ui_direct'))}
                      </span>
                      <div style={{ flex: 1, height: 1, background: 'rgba(255,255,255,0.08)' }} />
                    </div>

                    {rule.use_intermediate && (
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, minWidth: 0, background: 'rgba(245, 158, 11, 0.08)', padding: '6px 10px', borderRadius: 6 }}>
                        <div style={{ minWidth: 0, flex: 1 }}>
                          <div style={{ color: '#fbbf24', fontSize: '0.68rem' }}>{t('ui_hop_c')}</div>
                          <div style={{ fontWeight: 600, color: '#fbbf24', fontSize: '0.8rem', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {rule.intermediate_channel_name || rule.intermediate_channel_id}
                          </div>
                        </div>
                        <span className="font-mono-ltr channel-id-mono" style={{ fontSize: '0.7rem', color: '#b45309', flexShrink: 0, marginInlineStart: 8, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '46%' }}>
                          {rule.intermediate_channel_id}
                        </span>
                      </div>
                    )}

                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, minWidth: 0 }}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.68rem' }}>{t('ui_target_b')}</div>
                        <div style={{ fontWeight: 600, color: '#f8fafc', fontSize: '0.82rem', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {rule.target_chat_name || rule.target_chat_id}
                        </div>
                      </div>
                      <span className="font-mono-ltr channel-id-mono" style={{ fontSize: '0.72rem', color: '#64748b', flexShrink: 0, marginInlineStart: 8, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '46%' }}>
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
                      <div style={{ color: '#94a3b8', fontSize: '0.7rem' }}>{t('ui_source_a')}</div>
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
                          <span style={{ fontSize: '0.65rem' }}>{t('ui_hop')}</span>
                          <ArrowRightLeft size={14} />
                        </div>
                        <div style={{ flex: 1, minWidth: 0, textAlign: 'center' }}>
                          <div style={{ color: '#fbbf24', fontSize: '0.7rem' }}>{t('ui_brand_c')}</div>
                          <div style={{ fontWeight: 600, color: '#fbbf24', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {rule.intermediate_channel_name || rule.intermediate_channel_id || 'Channel C'}
                          </div>
                          <div style={{ fontSize: '0.7rem', color: '#b45309' }}>
                            <span className="font-mono-ltr">{rule.intermediate_channel_id}</span>
                          </div>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: '#10b981', flexShrink: 0 }}>
                          <span style={{ fontSize: '0.65rem' }}>{t('ui_native')}</span>
                          {isRtl ? <ChevronLeft size={14} /> : <ChevronRight size={14} />}
                        </div>
                      </>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: '#3b82f6', padding: '0 8px', flexShrink: 0 }}>
                        <span style={{ fontSize: '0.65rem' }}>{t('ui_direct')}</span>
                        {isRtl ? <ChevronLeft size={14} /> : <ChevronRight size={14} />}
                      </div>
                    )}

                    <div style={{ flex: 1, minWidth: 0, textAlign: isRtl ? 'left' : 'right' }}>
                      <div style={{ color: '#94a3b8', fontSize: '0.7rem' }}>{t('ui_target_b')}</div>
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
                        <span style={{ color: '#94a3b8' }}>{t('ui_header')}</span>
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
                          <Filter size={9} /> {t('ui_filter')}
                        </span>
                      )}
                      {rule.ai_config_id && (
                        <span className="badge badge-vip" style={{ fontSize: '0.68rem', padding: '2px 6px' }}>
                          <Bot size={9} /> AI
                        </span>
                      )}
                      {rule.fallback_enabled && (
                        <span className="badge badge-gray" style={{ fontSize: '0.68rem', padding: '2px 6px', color: '#a7f3d0' }}>
                          <Shield size={9} /> {t('ui_fallback')} {rule.fallback_mode}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Live per-rule counters from the delivery pipeline */}
                  {liveStat && (
                    <div
                      className="rule-stats"
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
                      <div className="rule-stat" style={{ flex: 1, padding: '7px 6px', background: 'rgba(16, 185, 129, 0.10)', textAlign: 'center' }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.64rem', fontWeight: 600 }}>{t('ui_fwd')}</div>
                        <div style={{ fontWeight: 800, color: '#34d399', lineHeight: 1.35 }}>{liveStat.forwarded ?? 0}</div>
                      </div>
                      <div className="rule-stat" style={{ flex: 1, padding: '7px 6px', background: 'rgba(59, 130, 246, 0.10)', textAlign: 'center' }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.64rem', fontWeight: 600 }}>{t('ui_filt')}</div>
                        <div style={{ fontWeight: 800, color: '#60a5fa', lineHeight: 1.35 }}>{liveStat.filtered ?? 0}</div>
                      </div>
                      <div className="rule-stat" style={{ flex: 1, padding: '7px 6px', background: liveStat.errors > 0 ? 'rgba(239, 68, 68, 0.18)' : 'rgba(239, 68, 68, 0.07)', textAlign: 'center' }}>
                        <div style={{ color: '#94a3b8', fontSize: '0.64rem', fontWeight: 600 }}>{t('ui_err')}</div>
                        <div style={{ fontWeight: 800, color: liveStat.errors > 0 ? '#fca5a5' : '#64748b', lineHeight: 1.35 }}>{liveStat.errors ?? 0}</div>
                      </div>
                      <div
                        className="rule-stat-last"
                        style={{
                          flex: 1.3,
                          padding: '7px 6px',
                          background: 'rgba(99, 102, 241, 0.06)',
                          textAlign: 'center',
                        }}
                      >
                        <span style={{ color: '#64748b', fontSize: '0.62rem' }}>{t('ui_last')}</span>
                        <span style={{ fontWeight: 700, color: '#818cf8', fontSize: '0.7rem' }}>
                          {liveStat.last_forward_ts
                            ? new Date(liveStat.last_forward_ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
                            : '—'}
                        </span>
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
                        <span>{t('ui_route_1_direct')}</span>
                      </button>

                      <button
                        onClick={() => handleQuickRouteSwitch(rule.id, 'route2_vip_hop')}
                        className={`route-switch-btn btn ${isVipHop ? 'btn-vip' : 'btn-secondary'}`}
                      >
                        <Sparkles size={11} />
                        <span>{t('ui_route_2_vip')}</span>
                      </button>

                      <button
                        onClick={() => handleQuickRouteSwitch(rule.id, 'route3_native')}
                        className={`route-switch-btn btn ${isNative ? 'btn-primary' : 'btn-secondary'}`}
                      >
                        <Send size={11} />
                        <span>{t('ui_route_3_native')}</span>
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
        <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))', gap: 24 }}>
          {/* Controls Panel */}
          <div className="glass-panel" style={{ padding: 24 }}>
            <h2 style={{ fontSize: '1.2rem', fontWeight: 700, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
              <PlayCircle size={20} color="#fbbf24" />
              <span>{t('ui_live_smart_simulator')}</span>
            </h2>
            <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginBottom: 20 }}>
              {t('ui_test_routing_engine_decisions_vip_origin_criteria_and_protected')}
            </p>

            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                {t('ui_select_target_rule')}
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
                {t('ui_simulated_message_text')}
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
                  {t('ui_forward_origin_chat_id')}
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
                  {t('ui_forward_origin_title')}
                </label>
                <input
                  type="text"
                  value={simOriginTitle}
                  onChange={(e) => setSimOriginTitle(e.target.value)}
                  className="input-field"
                  placeholder={t('ui_forex_vip_channel')}
                />
              </div>
            </div>

            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                {t('ui_forward_origin_username')}
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
                  {t('ui_protected_content_channel')}
                </span>
              </label>

              <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: '0.85rem' }}>
                <input
                  type="checkbox"
                  checked={simHasMedia}
                  onChange={(e) => setSimHasMedia(e.target.checked)}
                />
                <span>{t('ui_has_media')}</span>
              </label>
            </div>

            <button
              onClick={handleRunSimulation}
              disabled={simulating}
              className="btn btn-vip"
              style={{ width: '100%', padding: '12px' }}
            >
              <Zap size={18} />
              <span>{simulating ? (t('ui_simulating')) : (t('ui_run_decision_engine'))}</span>
            </button>
          </div>

          {/* Results Panel */}
          <div className="glass-panel" style={{ padding: 24 }}>
            <h3 style={{ fontSize: '1.1rem', fontWeight: 700, marginBottom: 14 }}>
              {t('ui_engine_decision_workflow_trace')}
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
                    <div style={{ fontSize: '0.75rem', opacity: 0.8 }}>{t('ui_decision')}</div>
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
                    {t('ui_decision_reason')}
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
                      {t('ui_bypass_activated_protected_content_will_be_re_uploaded_directly')}
                    </span>
                  </div>
                )}

                <div style={{ marginBottom: 16 }}>
                  <div style={{ fontSize: '0.8rem', color: '#94a3b8', marginBottom: 6 }}>
                    {t('ui_pipeline_execution_steps')}
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
                      {t('ui_output_message_preview')}
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
                <p>{t('ui_no_simulation_run_yet_click_run_to_evaluate')}</p>
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
                {t('ui_ai_intelligence_rewriting_engines')}
              </h2>
              <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                {t('ui_configure_llm_models_for_auto_rewriting_signal_parsing_and_trans')}
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
              <span>{t('ui_new_ai_engine')}</span>
            </button>
          </div>

          <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 20 }}>
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
                    <span style={{ color: '#94a3b8' }}>{t('ui_api_key')}</span>
                    <code>{ai.api_key_masked || (t('ui_none_environment'))}</code>
                  </div>
                  <div style={{ overflowWrap: 'anywhere' }}>
                    <span style={{ color: '#94a3b8' }}>
                      {t('ui_endpoint')}
                    </span>
                    <code dir="ltr">{ai.base_url || (t('ui_provider_default'))}</code>
                  </div>
                  <ProviderKeyCheck aiConfigId={ai.id} lang={lang} compact />
                  <div>
                    <span style={{ color: '#94a3b8' }}>{t('ui_temperature')}</span>
                    <span>{ai.temperature}</span> | <span style={{ color: '#94a3b8' }}>{t('ui_lang')}</span>
                    <span>{ai.target_language}</span>
                  </div>
                  {ai.system_prompt && (
                    <div style={{ background: 'rgba(15, 23, 42, 0.6)', padding: 8, borderRadius: 6, marginTop: 4 }}>
                      <span style={{ color: '#94a3b8', fontSize: '0.75rem', display: 'block' }}>{t('ui_system_prompt')}</span>
                      <span style={{ fontSize: '0.75rem' }}>{ai.system_prompt}</span>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* PROMPT EDITOR & TESTER (AI Rewrite) */}
      {activeTab === 'ai' && (
        <PromptEditorPanel
          prompts={Array.isArray(promptTemplates) ? promptTemplates : []}
          aiConfigs={Array.isArray(aiConfigs) ? aiConfigs : []}
          onSaved={async () => setPromptTemplates(await api.getPromptTemplates().catch(() => []))}
          onToast={showToast}
          lang={lang}
        />
      )}

      {/* TAB: AI PV ASSISTANT */}
      {activeTab === 'pv' && (
        <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))', gap: 24 }}>
          {/* Main Controls & Prompt */}
          <div className="glass-panel" style={{ padding: 24 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <div>
                <h2 style={{ fontSize: '1.25rem', fontWeight: 800, display: 'flex', alignItems: 'center', gap: 10 }}>
                  <MessageSquare size={22} color="#ec4899" />
                  <span>{t('ui_ai_pv_assistant')}</span>
                </h2>
                <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                  {t('ui_human_like_conversational_auto_responder_for_telegram_private_me')}
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
                <span>{pvConfig?.enabled ? (t('ui_enabled')) : (t('ui_disabled'))}</span>
              </button>
            </div>

            {/* Persona Preset Buttons */}
            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 8, fontWeight: 600 }}>
                {t('ui_persona_tone_presets')}
              </label>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 8 }}>
                <button
                  type="button"
                  onClick={() => {
                    setPvPreset('casual')
                    setPvConfig((prev) => prev ? { ...prev, persona_prompt: t('ui_pv_preset_casual_prompt') } : null)
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
                  {t('ui_pv_preset_casual_label')}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setPvPreset('business')
                    setPvConfig((prev) => prev ? { ...prev, persona_prompt: t('ui_pv_preset_business_prompt') } : null)
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
                  {t('ui_pv_preset_business_label')}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setPvPreset('short')
                    setPvConfig((prev) => prev ? { ...prev, persona_prompt: t('ui_pv_preset_short_prompt') } : null)
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
                  {t('ui_pv_preset_short_label')}
                </button>
              </div>
            </div>

            {/* Persona Prompt Textarea */}
            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6, fontWeight: 600 }}>
                {t('ui_persona_prompt_instructions')}
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
                placeholder={t('ui_ai_behavior_prompt_for_private_chats')}
              />
            </div>

            {/* Connect to AI Config */}
            <div style={{ marginBottom: 16 }}>
              <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                {t('ui_linked_ai_model_configuration')}
              </label>
              <select
                value={pvConfig?.ai_config_id || ''}
                onChange={(e) => setPvConfig((prev) => prev ? { ...prev, ai_config_id: e.target.value || undefined } : null)}
                className="input-field"
                style={{ width: '100%' }}
              >
                <option value="">{t('ui_system_default')}</option>
                {aiConfigs.map((ai) => (
                  <option key={ai.id} value={ai.id}>
                    {ai.name} ({ai.provider} - {ai.model})
                  </option>
                ))}
              </select>
            </div>

            {/* Tuning Settings Grid */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 12, marginBottom: 18 }}>
              <div>
                <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {t('ui_min_typing_delay_s')}
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
                  {t('ui_max_typing_delay_s')}
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
                  {t('ui_cooldown_s')}
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
                  {t('ui_chat_history_limit')}
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
                <span>{t('ui_ignore_bot_messages')}</span>
              </label>

              <button
                type="button"
                onClick={() => handleSavePV()}
                disabled={pvSaving}
                className="btn btn-primary"
                style={{ background: '#ec4899', borderColor: '#db2777' }}
              >
                <Check size={16} />
                <span>{pvSaving ? (t('ui_saving')) : (t('ui_save_pv_config'))}</span>
              </button>
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
                {t('ui_advanced_content_media_filtering')}
              </h2>
              <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                {t('ui_manage_text_blacklists_whitelists_regex_patterns_and_media_type')}
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
              <span>{t('ui_new_filter_rule')}</span>
            </button>
          </div>

          <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 20 }}>
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
                      {t('ui_blacklist')}
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
                      {t('ui_allowed_media')}
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
              {t('ui_telegram_mtproto_client_sessions')}
            </h2>
            <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
              {t('ui_monitor_connected_userbot_sessions_auth_status_and_proxies')}
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
                    {sess.is_active ? (t('ui_online')) : (t('ui_offline'))}
                  </span>
                </div>

                <div style={{ fontSize: '0.8rem', color: '#cbd5e1', display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 14 }}>
                  <div>
                    <span style={{ color: '#94a3b8' }}>{t('ui_phone')}</span>
                    <span>{sess.phone_number || (t('ui_n_a'))}</span>
                  </div>
                  <div>
                    <span style={{ color: '#94a3b8' }}>{t('ui_user_id')}</span>
                    <span style={{ fontFamily: 'monospace' }}>{sess.user_id}</span>
                  </div>
                  <div>
                    <span style={{ color: '#94a3b8' }}>{t('ui_auth_status')}</span>
                    <span style={{ color: sess.is_authorized ? '#34d399' : '#f87171' }}>
                      {sess.is_authorized ? (t('ui_authorized')) : (t('ui_unauthorized'))}
                    </span>
                  </div>
                  <div>
                    <span style={{ color: '#94a3b8' }}>{t('ui_proxy')}</span>
                    <span>{sess.proxy || (t('ui_direct_no_proxy'))}</span>
                  </div>
                </div>

                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', borderTop: '1px solid rgba(255,255,255,0.06)', paddingTop: 12 }}>
                  <button
                    onClick={() => handleBackupSession(sess)}
                    className="btn btn-secondary"
                    style={{ flex: 1, fontSize: '0.75rem', padding: '6px 8px', minWidth: 110 }}
                  >
                    <Layers size={13} />
                    <span>{t('ui_backup')}</span>
                  </button>
                  <button
                    onClick={() => handleTerminateSession(sess)}
                    className="btn btn-danger"
                    style={{ flex: 1, fontSize: '0.75rem', padding: '6px 8px', minWidth: 110 }}
                  >
                    <X size={13} />
                    <span>{t('ui_terminate')}</span>
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
                {t('ui_delivery_jobs_dead_letter_queue_dlq')}
              </h2>
              <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                {t('ui_track_in_flight_delivery_jobs_and_recover_failed_telegram_messag')}
              </p>
            </div>
            {dlqJobs.length > 0 && (
              <button onClick={handlePurgeDLQ} className="btn btn-danger">
                <Trash2 size={16} />
                <span>{t('ui_purge_all_dlq')}</span>
              </button>
            )}
          </div>

          {/* Delivery totals — the same snapshot the overview cards read, shown
              here so the queue tab answers "how much moved?" at a glance. */}
          {deliveryStats && (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
                gap: 12,
                marginBottom: 20,
              }}
            >
              {[
                { label: t('ui_processed'), value: deliveryStats.processed_total ?? 0, color: '#818cf8' },
                { label: t('ui_forwarded'), value: deliveryStats.forwarded_total ?? 0, color: '#34d399' },
                { label: t('ui_failed'), value: deliveryStats.failed_total ?? 0, color: '#f87171' },
                { label: t('ui_filtered'), value: deliveryStats.filtered_total ?? 0, color: '#fbbf24' },
                { label: t('ui_in_queue'), value: deliveryStats.in_queue ?? 0, color: '#38bdf8' },
              ].map((m) => (
                <div key={m.label} className="glass-panel" style={{ padding: '10px 14px' }}>
                  <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>{m.label}</div>
                  <div style={{ fontSize: '1.15rem', fontWeight: 800, color: m.color }}>{m.value}</div>
                </div>
              ))}
            </div>
          )}

          <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))', gap: 24 }}>
            {/* Active Delivery Queue */}
            <div className="glass-panel" style={{ padding: 20 }}>
              <h3 style={{ fontSize: '1rem', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                <Clock size={16} color="#818cf8" />
                <span>{t('ui_recent_delivery_jobs')}</span>
              </h3>

              {queueJobs.length === 0 ? (
                <div style={{ textAlign: 'center', padding: 30, color: '#64748b', fontSize: '0.85rem' }}>
                  {t('ui_no_pending_jobs_in_queue')}
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
                        <div style={{ fontWeight: 600 }}>{t('ui_msg')} #{q.source_message_id}</div>
                        <div style={{ color: '#94a3b8', fontSize: '0.75rem' }}>{t('ui_stage')} {q.delivery_stage}</div>
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
                <span>{t('ui_dead_letter_queue_dlq')}</span>
                <span className="badge badge-vip">{dlqJobs.length}</span>
              </h3>

              {dlqJobs.length === 0 ? (
                <div style={{ textAlign: 'center', padding: 30, color: '#64748b', fontSize: '0.85rem' }}>
                  {t('ui_dead_letter_queue_is_empty')}
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
                          <span>{t('ui_retry')}</span>
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
                    background: logAutoRefresh ? '#10b981' : '#64748b',
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
                      fontSize: '0.72rem',
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
                            <span style={{ fontSize: '0.68rem', color: '#64748b' }}>
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
                              <div style={{ fontSize: '0.7rem', color: '#64748b', fontFamily: 'monospace' }} dir="ltr">{n.sub}</div>
                            </div>
                          </div>
                          <div style={{ textAlign: 'right', minWidth: 0, maxWidth: '100%' }} dir="ltr">
                            <div style={{ fontSize: '0.72rem', fontWeight: 700, color: online ? '#34d399' : '#f87171' }}>
                              {online ? (t('ui_online')) : (t('ui_down'))}
                              {s && s.latency_ms > 0 ? ` · ${s.latency_ms}ms` : ''}
                            </div>
                            {s?.detail && (
                              <div style={{ fontSize: '0.66rem', color: '#64748b', maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
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
                    <option value="core">core (پایتون هسته)</option>
                    <option value="bot">bot (ربات تلگرام)</option>
                    <option value="api">api (کنترل پلین Go)</option>
                    <option value="pipeline">pipeline (پایپ‌لاین فوروارد)</option>
                    <option value="boot">boot (راه‌اندازی سشن‌ها)</option>
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

              {/* Text Search Input */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 260, flex: 1, maxWidth: 380 }}>
                <input
                  type="text"
                  placeholder={t('ui_search_logs_live')}
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
                className="btn btn-secondary" style={{ padding: '5px 12px', fontSize: '0.72rem' }}
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
                className="btn btn-secondary" style={{ padding: '5px 12px', fontSize: '0.72rem' }}
              >
                ⬇ {t('ui_older')}
              </button>
              <label style={{ fontSize: '0.72rem', color: '#cbd5e1', display: 'flex', alignItems: 'center', gap: 6 }}>
                {t('ui_page_size')}
                <select
                  value={logPageSize}
                  onChange={(e) => {
                    const size = Number(e.target.value)
                    setLogPageSize(size)
                    setLogOffset(0)
                    fetchLogs(logFilterService, logFilterLevel, logSearch, { limit: size, offset: 0 })
                  }}
                  style={{ background: '#0b1220', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 6, padding: '4px 6px', fontSize: '0.72rem' }}
                >
                  {[50, 100, 200, 500].map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </label>
              <span style={{ fontSize: '0.68rem', color: logHasMore ? '#fbbf24' : '#34d399' }}>
                {logHasMore
                  ? (t('ui_older_events_exist'))
                  : (t('ui_reached_the_end_of_history'))}
              </span>
              <button
                onClick={() => handleExportLogs('json')} disabled={logExporting}
                className="btn btn-secondary" style={{ padding: '5px 12px', fontSize: '0.72rem' }}
              >
                {logExporting ? '⏳ ...' : '⬇ JSON'}
              </button>
              <button
                onClick={() => handleExportLogs('csv')} disabled={logExporting}
                className="btn btn-secondary" style={{ padding: '5px 12px', fontSize: '0.72rem' }}
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
              <label style={{ fontSize: '0.72rem', color: '#cbd5e1', display: 'flex', alignItems: 'center', gap: 6 }}>
                {t('ui_keep_last')}
                <input
                  type="number" min={0} max={720} step={1} value={purgeWindowH}
                  onChange={(e) => setPurgeWindowH(Math.max(0, Math.min(720, Number(e.target.value) || 0)))}
                  style={{ width: 64, background: '#0b1220', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 6, padding: '4px 6px', fontSize: '0.72rem' }}
                />
                {t('ui_h')}
              </label>
              {(logFilterLevel !== 'all' || logFilterService !== 'all') && (
                <span className="badge badge-gray" style={{ fontSize: '0.68rem' }}>
                  {t('ui_scoped')} {[logFilterLevel !== 'all' && logFilterLevel, logFilterService !== 'all' && logFilterService].filter(Boolean).join(' · ')}
                </span>
              )}
              <button
                onClick={handlePurgeLogs} disabled={purging}
                className="btn btn-secondary" style={{ padding: '5px 12px', fontSize: '0.72rem' }}
              >
                {purging ? '⏳ ...' : (t('ui_purge_old_logs'))}
              </button>
              {purgeResult && (
                <span style={{ fontSize: '0.7rem', color: '#34d399' }}>
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
                  <label style={{ fontSize: '0.72rem', color: '#94a3b8', display: 'block', marginBottom: 4 }}>
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
                    <div style={{ marginTop: 10, fontSize: '0.7rem', color: '#94a3b8' }}>
                      {backupEncrypted && (
                        <span className="badge" style={{ background: 'rgba(16,185,129,0.15)', color: '#34d399', fontSize: '0.66rem', marginRight: 6 }}>🔒 AES-GCM</span>
                      )}
                      {Object.entries(backupCounts).map(([k, v]) => (
                        <span key={k} style={{ display: 'inline-block', margin: '2px 4px 0 0', padding: '2px 7px', background: 'rgba(255,255,255,0.05)', borderRadius: 6 }}>
                          {k}: <b style={{ color: '#e2e8f0' }}>{v}</b>
                        </span>
                      ))}
                    </div>
                  )}
                  {!backupEncrypted && backupCounts && (
                    <div style={{ marginTop: 8, fontSize: '0.68rem', color: '#fbbf24' }}>
                      ⚠️ {t('ui_this_file_is_unencrypted_and_holds_live_credentials')}
                    </div>
                  )}
                </div>

                {/* IMPORT */}
                <div style={{ background: 'rgba(15, 23, 42, 0.5)', border: '1px solid rgba(168, 85, 247, 0.18)', borderRadius: 10, padding: 14 }}>
                  <div style={{ fontSize: '0.82rem', fontWeight: 700, color: '#c4b5fd', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
                    <Upload size={14} /> {t('ui_2_restore_on_this_server')}
                  </div>
                  <label style={{ fontSize: '0.72rem', color: '#94a3b8', display: 'block', marginBottom: 4 }}>
                    {t('ui_backup_file')}
                  </label>
                  <input
                    type="file" accept=".json,.enc,application/json"
                    onChange={(e) => onBackupFileChosen(e.target.files?.[0])}
                    style={{ width: '100%', boxSizing: 'border-box', fontSize: '0.72rem', color: '#94a3b8', marginBottom: 8 }}
                  />
                  <label style={{ fontSize: '0.72rem', color: '#94a3b8', display: 'block', marginBottom: 4 }}>
                    {t('ui_or_paste_the_bundle')}
                  </label>
                  <textarea
                    value={importText}
                    onChange={(e) => { setImportText(e.target.value); setImportStats(null) }}
                    placeholder={t('ui_bundle_contents')}
                    rows={3}
                    style={{ width: '100%', boxSizing: 'border-box', background: '#0b1220', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 8, padding: '7px 10px', fontSize: '0.7rem', fontFamily: 'monospace', resize: 'vertical', marginBottom: 10 }}
                  />
                  <label style={{ fontSize: '0.72rem', color: '#94a3b8', display: 'block', marginBottom: 4 }}>
                    {t('ui_passphrase_if_the_file_is_encrypted')}
                  </label>
                  <input
                    type="password"
                    value={importPassphrase}
                    onChange={(e) => setImportPassphrase(e.target.value)}
                    placeholder={t('ui_passphrase')}
                    style={{ width: '100%', boxSizing: 'border-box', background: '#0b1220', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 8, padding: '7px 10px', fontSize: '0.78rem', marginBottom: 10 }}
                  />
                  <label style={{ fontSize: '0.72rem', color: '#cbd5e1', display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10 }}>
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
                        <span key={k} style={{ fontSize: '0.66rem', padding: '3px 8px', borderRadius: 6, background: 'rgba(255,255,255,0.05)', color: '#94a3b8' }}>
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
                <div style={{ textAlign: 'center', padding: '40px 20px', color: '#64748b' }}>
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
                                fontSize: '0.68rem',
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
                                fontSize: '0.68rem',
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
                          title={t('ui_copy_log_line')}
                        >
                          {copiedLogId === logId ? (
                            <>
                              <Check size={12} />
                              <span style={{ fontSize: '0.65rem' }}>{t('ui_copied')}</span>
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
                  <span className="badge badge-vip" style={{ fontSize: '0.7rem' }}>{recentErrors.length}</span>
                )}
              </h3>

              {(!Array.isArray(recentErrors) || recentErrors.length === 0) ? (
                <div style={{ textAlign: 'center', padding: 26, color: '#64748b', fontSize: '0.85rem' }}>
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

      {/* TAB 8: ADMIN CONTROL PLANE */}
      {activeTab === 'jobs' && (
        <JobsPanel
          lang={lang}
          onPendingChange={setJobsPending}
        />
      )}

      {activeTab === 'audit' && (
        <AuditEventsPanel lang={lang} />
      )}

      {/* TAB 9: ADMIN */}
      {activeTab === 'admin' && currentUser?.is_admin && (
        <AdminPanel lang={lang} />
      )}

      {/* TAB 9: CLIENT SETTINGS */}
      {activeTab === 'settings' && (
        <ClientSettings lang={lang} />
      )}

      {/* 1:1 PARITY TASK OVERVIEW & ADVANCED SETTINGS MODAL */}
      <TaskOverviewModal
        rule={taskOverviewRule}
        isOpen={isTaskOverviewOpen}
        onClose={() => setIsTaskOverviewOpen(false)}
        onSave={handleSaveTaskOverview}
        onDelete={handleDeleteRule}
        lang={lang}
      />

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
                {ruleFormData.id ? (t('ui_edit_forward_rule')) : (t('ui_new_forward_rule'))}
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
                    {t('ui_source_chat_id_a')} *
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
                    {t('ui_source_title')}
                  </label>
                  <input
                    type="text"
                    value={ruleFormData.source_chat_name || ''}
                    onChange={(e) => setRuleFormData({ ...ruleFormData, source_chat_name: e.target.value })}
                    className="input-field"
                    placeholder={t('ui_forex_signals')}
                  />
                </div>
              </div>

              <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {t('ui_target_chat_id_b')} *
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
                    {t('ui_target_title')}
                  </label>
                  <input
                    type="text"
                    value={ruleFormData.target_chat_name || ''}
                    onChange={(e) => setRuleFormData({ ...ruleFormData, target_chat_name: e.target.value })}
                    className="input-field"
                    placeholder={t('ui_usdjpy_vip')}
                  />
                </div>
              </div>

              {/* Loop Detection Real-time Warning */}
              {(() => {
                const normEp = (s?: string) => (s || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
                const s = normEp(ruleFormData.source_chat_id)
                const targetId = normEp(ruleFormData.target_chat_id)
                const isLoop = Boolean(s && targetId && s === targetId)
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
                      {t('ui_routing_loop_detected_source_and_target_chat_ids_cannot_be_ident')}
                    </span>
                  </div>
                )
              })()}

              {(() => {
                if (!ruleFormData.use_intermediate || !ruleFormData.intermediate_channel_id) return null
                const normEp = (s?: string) => (s || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
                const s = normEp(ruleFormData.source_chat_id)
                const targetId = normEp(ruleFormData.target_chat_id)
                const im = normEp(ruleFormData.intermediate_channel_id)
                if (im && (im === s || im === targetId)) {
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
                        {t('ui_routing_loop_detected_intermediate_hop_channel_cannot_match_sour')}
                      </span>
                    </div>
                  )
                }
                return null
              })()}

              {/* Branding hop (A → C → B). The enable switch and channel C are
                  rule fields; the trigger configuration is metadata.branding_hop. */}
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
                    {t('ui_use_intermediate_branding_channel_hop_via_c')}
                  </span>
                </label>

                {ruleFormData.use_intermediate && (
                  <>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                      <div>
                        <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                          {t('ui_intermediate_channel_id_c')}
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
                          {t('ui_intermediate_title')}
                        </label>
                        <input
                          type="text"
                          value={ruleFormData.intermediate_channel_name || ''}
                          onChange={(e) => setRuleFormData({ ...ruleFormData, intermediate_channel_name: e.target.value })}
                          className="input-field"
                          placeholder={t('ui_my_brand_channel')}
                        />
                      </div>
                    </div>

                    {/* Hop triggers — stored in the rule metadata under branding_hop */}
                    <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px solid rgba(245, 158, 11, 0.25)' }}>
                      <div style={{ fontSize: '0.78rem', fontWeight: 700, color: '#fbbf24', marginBottom: 8 }}>
                        {t('ui_branding_hop_triggers_optional')}
                      </div>

                      <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: '0.78rem', color: '#cbd5e1', marginBottom: 10 }}>
                        <input
                          type="checkbox"
                          checked={brandingHop.triggers_enabled}
                          onChange={(e) => patchBrandingHop({ triggers_enabled: e.target.checked })}
                        />
                        <span>
                          {t('ui_only_hop_messages_matching_the_triggers_below_otherwise_every_ma')}
                        </span>
                      </label>

                      {brandingHop.triggers_enabled && (
                        <>
                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                            <div>
                              <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                                {t('ui_forward_origin_channel_names')}
                              </label>
                              <input
                                type="text"
                                value={brandingListValue('origin_titles')}
                                onChange={(e) => onBrandingListInput('origin_titles', e.target.value)}
                                className="input-field"
                                placeholder={t('ui_premium_deals_forex_vip')}
                              />
                              <div style={{ fontSize: '0.7rem', color: '#64748b', marginTop: 4 }}>
                                {t('ui_comma_separated_matches_the_channel_name_in_the_forward_header_n')}
                              </div>
                            </div>
                            <div>
                              <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                                {t('ui_forward_origin_channel_ids')}
                              </label>
                              <input
                                type="text"
                                value={brandingListValue('origin_ids')}
                                onChange={(e) => onBrandingListInput('origin_ids', e.target.value)}
                                className="input-field"
                                placeholder="-1001111111111, @premium_deals"
                              />
                            </div>
                          </div>

                          <div style={{ marginTop: 12 }}>
                            <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                              {t('ui_message_text_keywords')}
                            </label>
                            <input
                              type="text"
                              value={brandingListValue('keywords')}
                              onChange={(e) => onBrandingListInput('keywords', e.target.value)}
                              className="input-field"
                              placeholder="BUY, SIGNAL, طلا"
                            />
                          </div>

                          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
                            <div>
                              <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                                {t('ui_trigger_match_mode')}
                              </label>
                              <select
                                value={brandingHop.match_mode}
                                onChange={(e) => patchBrandingHop({ match_mode: e.target.value === 'all' ? 'all' : 'any' })}
                                className="input-field"
                                style={{ width: '100%' }}
                              >
                                <option value="any">{t('ui_any_one_matching_group_is_enough')}</option>
                                <option value="all">{t('ui_all_every_matching_group_must_match')}</option>
                              </select>
                            </div>
                            <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: '0.78rem', color: '#cbd5e1', marginTop: 18 }}>
                              <input
                                type="checkbox"
                                checked={brandingHop.require_forward}
                                onChange={(e) => patchBrandingHop({ require_forward: e.target.checked })}
                              />
                              <span>{t('ui_refuse_messages_that_are_not_forwards')}</span>
                            </label>
                          </div>
                        </>
                      )}

                      <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: '0.78rem', color: '#cbd5e1', marginTop: 12 }}>
                        <input
                          type="checkbox"
                          checked={brandingHop.force_forward}
                          onChange={(e) => patchBrandingHop({ force_forward: e.target.checked })}
                        />
                        <span>
                          {t('ui_native_forward_from_c_the_only_way_b_shows_c_in_the_header')}
                        </span>
                      </label>

                      <div
                        dir={isRtl ? 'rtl' : 'ltr'}
                        style={{
                          marginTop: 10,
                          padding: '8px 10px',
                          background: 'rgba(245, 158, 11, 0.08)',
                          border: '1px dashed rgba(245, 158, 11, 0.35)',
                          borderRadius: 8,
                          fontSize: '0.75rem',
                          color: '#fcd34d',
                          lineHeight: 1.6,
                          wordBreak: 'break-word',
                        }}
                      >
                        {brandingHopSummary(
                          lang,
                          brandingHop,
                          `${ruleFormData.source_chat_name || ruleFormData.source_chat_id || 'A'} → ${ruleFormData.intermediate_channel_name || ruleFormData.intermediate_channel_id || 'C'} → ${ruleFormData.target_chat_name || ruleFormData.target_chat_id || 'B'}`,
                        )}
                      </div>
                    </div>
                  </>
                )}
              </div>

              {/* Custom Header & Footer */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {t('ui_custom_header')}
                  </label>
                  <input
                    type="text"
                    value={ruleFormData.custom_header || ''}
                    onChange={(e) => setRuleFormData({ ...ruleFormData, custom_header: e.target.value })}
                    className="input-field"
                    placeholder={t('ui_vip_forex')}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {t('ui_custom_footer')}
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
                  {t('ui_media_sync_settings_bot_parity')}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 10 }}>
                  {([
                    ['block_voice', '🎙', t('ui_block_voice')],
                    ['block_stickers', '🎭', t('ui_block_stickers')],
                    ['block_video', '🎬', t('ui_block_video')],
                    ['block_audio', '🎵', t('ui_block_audio')],
                    ['block_text', '📝', t('ui_block_text')],
                    ['remove_emojis', '😀', t('ui_strip_emojis')],
                    ['sync_deletes', '🗑', t('ui_sync_deletes')],
                    ['ignore_edits', '✏️', t('ui_ignore_edits')],
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
                    {t('ui_album_mode')}
                  </label>
                  <select
                    value={ruleMeta.album_mode || 'album'}
                    onChange={(e) => setRuleMeta({ ...ruleMeta, album_mode: e.target.value })}
                    className="input-field"
                    style={{ width: '100%' }}
                  >
                    <option value="album">{t('ui_full_album')}</option>
                    <option value="first">{t('ui_first_media_only')}</option>
                    <option value="split">{t('ui_split_messages')}</option>
                  </select>
                </div>
              </div>

              {/* AI Rewrite (per-rule) */}
              <div style={{ marginTop: 18, paddingTop: 16, borderTop: '1px solid rgba(255,255,255,0.06)' }}>
                <div style={{ fontSize: '0.8rem', fontWeight: 800, color: '#c084fc', marginBottom: 10 }}>
                  🤖 {t('ui_ai_message_rewrite')}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                  <div>
                    <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                      {t('ui_ai_config')}
                    </label>
                    <select
                      value={ruleFormData.ai_config_id || ''}
                      onChange={(e) => setRuleFormData({ ...ruleFormData, ai_config_id: e.target.value || undefined })}
                      className="input-field"
                      style={{ width: '100%' }}
                    >
                      <option value="">{t('ui_disabled_no_rewrite')}</option>
                      {(Array.isArray(aiConfigs) ? aiConfigs : []).map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.is_enabled ? '🟢' : '🔴'} {a.name} [{a.provider}/{a.model}]
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                      {t('ui_rewrite_prompt')}
                    </label>
                    <select
                      value={ruleMeta.prompt_id || ''}
                      onChange={(e) => setRuleMeta({ ...ruleMeta, prompt_id: e.target.value || undefined })}
                      className="input-field"
                      style={{ width: '100%' }}
                    >
                      <option value="">{t('ui_default_no_system_prompt')}</option>
                      {(Array.isArray(promptTemplates) ? promptTemplates : []).map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.is_system ? '⭐' : '👤'} {p.name} (v{p.current_version})
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                      {t('ui_on_ai_failure')}
                    </label>
                    <select
                      value={ruleFormData.ai_fallback_policy || 'DROP'}
                      onChange={(e) => setRuleFormData({ ...ruleFormData, ai_fallback_policy: e.target.value })}
                      className="input-field"
                      style={{ width: '100%' }}
                    >
                      <option value="SEND_ORIGINAL">{t('ui_send_original_fail_open')}</option>
                      <option value="DROP">{t('ui_drop_fail_closed')}</option>
                      <option value="RETRY">{t('ui_app_retry')}</option>
                      <option value="QUARANTINE">{t('ui_quarantine')}</option>
                    </select>
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                      {t('ui_timeout_s')}
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
                  {t('ui_fail_open_sends_the_original_text_if_ai_fails_and_logs_the_fallb')}
                </div>
              </div>

              <div className="modal-sheet-actions" style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 24 }}>
                <button type="button" onClick={() => setIsRuleModalOpen(false)} className="btn btn-secondary">
                  {t('ui_cancel')}
                </button>
                {(() => {
                  const normEp = (s?: string) => (s || '').trim().toLowerCase().replace(/^(https?:\/\/)?(t\.me\/)?@?/, '')
                  const s = normEp(ruleFormData.source_chat_id)
                  const targetId = normEp(ruleFormData.target_chat_id)
                  const im = normEp(ruleFormData.intermediate_channel_id)
                  const hasLoop = Boolean(s && targetId && s === targetId) ||
                    Boolean(ruleFormData.use_intermediate && im && (im === s || im === targetId))
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
                      <span>{t('ui_save_rule')}</span>
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
      {isAiModalOpen && (() => {
        // The catalogue is the core's list; this only reads it. `provider`
        // arrives from the API as the enum name (OPENAI) and the catalogue is
        // keyed by id (openai), so compare case-insensitively.
        const providerId = (aiFormData.provider || 'openai').toLowerCase()
        const providerMeta = aiProviders.find((p) => p.id === providerId)
        const endpointHint = providerMeta?.default_base_url || ''
        const modelIdeas = providerMeta?.models || []
        return (
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
                {t('ui_ai_engine_configuration')}
              </h3>
              <button onClick={() => setIsAiModalOpen(false)} className="btn btn-secondary" style={{ padding: 6 }}>
                <X size={16} />
              </button>
            </div>

            <form onSubmit={handleSaveAI}>
              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {t('ui_configuration_name')} *
                </label>
                <input
                  type="text"
                  required
                  value={aiFormData.name || ''}
                  onChange={(e) => setAiFormData({ ...aiFormData, name: e.target.value })}
                  className="input-field"
                  placeholder={t('ui_openai_gpt_4o_fast')}
                />
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {t('ui_provider')}
                  </label>
                  <select
                    value={providerId}
                    onChange={(e) => setAiFormData({ ...aiFormData, provider: e.target.value })}
                    className="input-field"
                  >
                    {(aiProviders.length
                      ? aiProviders
                      : [
                          { id: 'openai', label: 'OpenAI', default_base_url: '', models: [] },
                          { id: 'anthropic', label: 'Anthropic', default_base_url: '', models: [] },
                          { id: 'custom', label: 'Custom (OpenAI-compatible)', default_base_url: '', models: [] },
                        ]
                    ).map((prov) => (
                      <option key={prov.id} value={prov.id}>
                        {prov.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                    {t('ui_model_name')}
                  </label>
                  <input
                    type="text"
                    required
                    list="ai-model-suggestions"
                    value={aiFormData.model || ''}
                    onChange={(e) => setAiFormData({ ...aiFormData, model: e.target.value })}
                    className="input-field"
                    placeholder={modelIdeas[0] || 'gpt-4o-mini'}
                  />
                  <datalist id="ai-model-suggestions">
                    {modelIdeas.map((m) => (
                      <option key={m} value={m} />
                    ))}
                  </datalist>
                </div>
              </div>

              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {t('ui_provider_endpoint_base_url')}
                </label>
                <input
                  type="text"
                  value={aiFormData.base_url || ''}
                  onChange={(e) => setAiFormData({ ...aiFormData, base_url: e.target.value })}
                  className="input-field"
                  dir="ltr"
                  placeholder={endpointHint || 'https://api.example.com/v1'}
                />
                <span style={{ display: 'block', fontSize: '0.72rem', color: '#94a3b8', marginTop: 4 }}>
                  {endpointHint
                    ? t('ui_leave_blank_to_use_this_provider_s_default_endpointhint_2', { endpointHint_2: endpointHint })
                    : t('ui_for_a_custom_provider_enter_the_full_address_of_your_own_endpoin')}
                </span>
              </div>

              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {t('ui_app_api_key')}
                </label>
                <input
                  type="password"
                  value={aiFormData.api_key || ''}
                  onChange={(e) => setAiFormData({ ...aiFormData, api_key: e.target.value })}
                  className="input-field"
                  dir="ltr"
                  placeholder="sk-..."
                />
                <span style={{ display: 'block', fontSize: '0.72rem', color: '#94a3b8', marginTop: 4 }}>
                  {aiFormData.id
                    ? t('ui_leave_blank_to_keep_the_stored_key')
                    : t('ui_stored_on_the_server_only_it_is_never_sent_back_to_this_page')}
                </span>
                <div style={{ marginTop: 8 }}>
                  <ProviderKeyCheck
                    aiConfigId={aiFormData.id}
                    provider={providerId}
                    model={aiFormData.model}
                    baseUrl={aiFormData.base_url}
                    apiKey={aiFormData.api_key}
                    lang={lang}
                  />
                </div>
              </div>

              <div style={{ marginBottom: 16 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {t('ui_system_prompt')}
                </label>
                <textarea
                  rows={3}
                  value={aiFormData.system_prompt || ''}
                  onChange={(e) => setAiFormData({ ...aiFormData, system_prompt: e.target.value })}
                  className="input-field"
                  placeholder={t('ui_you_are_an_expert_forex_signal_translator')}
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
                <button type="button" onClick={() => setIsAiModalOpen(false)} className="btn btn-secondary">
                  {t('ui_cancel')}
                </button>
                <button type="submit" className="btn btn-primary">
                  <Check size={16} />
                  <span>{t('ui_save')}</span>
                </button>
              </div>
            </form>
          </div>
        </div>
        )
      })()}

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
                {t('ui_content_filter_rule')}
              </h3>
              <button onClick={() => setIsFilterModalOpen(false)} className="btn btn-secondary" style={{ padding: 6 }}>
                <X size={16} />
              </button>
            </div>

            <form onSubmit={handleSaveFilter}>
              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {t('ui_filter_name')} *
                </label>
                <input
                  type="text"
                  required
                  value={filterFormData.name || ''}
                  onChange={(e) => setFilterFormData({ ...filterFormData, name: e.target.value })}
                  className="input-field"
                  placeholder={t('ui_anti_spam_filter')}
                />
              </div>

              <div style={{ marginBottom: 12 }}>
                <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 4 }}>
                  {t('ui_blacklist_keywords_comma_separated')}
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
                  {t('ui_cancel')}
                </button>
                <button type="submit" className="btn btn-primary">
                  <Check size={16} />
                  <span>{t('ui_save_filter')}</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * v18 multi-source tasks forward from every listed channel, but only the
 * primary is rendered in the flow diagram. Surface the remainder as a count so
 * a task fed by 4 channels does not look identical to one fed by a single
 * channel.
 */
function extraSourceCount(rule: any): number {
  const ids: string[] = Array.isArray(rule?.source_chat_ids) ? rule.source_chat_ids.filter(Boolean) : []
  if (ids.length > 1) return ids.length - 1
  const names: string[] = Array.isArray(rule?.source_chat_names) ? rule.source_chat_names.filter(Boolean) : []
  return names.length > 1 ? names.length - 1 : 0
}

function UserBar({ lang }: { lang: UiLang }) {
  const t = makeT(lang)
  const [user, setUser] = useState<{ username: string; is_admin: boolean } | null>(null)

  useEffect(() => {
    api.me().then(setUser).catch(() => {})
  }, [])

  const handleLogout = () => {
    api.logout()
    // session-core is the one owner of the token: it removes the stored token
    // (by its real key, not a copy of the literal) and emits `session_cleared`.
    // `auth_required` is what Root listens to, so the dashboard still leaves.
    clearSession()
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
            {t('ui_role_admin')}
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
        {t('ui_logout')}
      </button>
    </div>
  )
}
export default App

// ===========================================================================
// AI Rewrite Prompt Editor & Tester (shared bot + dashboard logic via API)
// ===========================================================================
// ---------------------------------------------------------------- key check
//
// "Can this endpoint and key be used?" — asked before saving, and again for a
// stored config. The probe runs on the server (the key never leaves it) and the
// answer names its own refusal, so a tenant can fix the thing that is wrong
// instead of guessing.
function ProviderKeyCheck({
  aiConfigId,
  provider,
  model,
  baseUrl,
  apiKey,
  lang,
  compact,
}: {
  aiConfigId?: string
  provider?: string
  model?: string
  baseUrl?: string
  apiKey?: string
  lang: UiLang
  compact?: boolean
}) {
  const t = makeT(lang)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<CheckProviderKeyResponse | null>(null)
  const [failure, setFailure] = useState('')
  const view = describeKeyCheck(result, lang, !(apiKey || '').trim())

  const run = async () => {
    setBusy(true)
    setFailure('')
    setResult(null)
    try {
      setResult(
        await api.checkProviderKey({
          provider,
          model,
          base_url: baseUrl,
          api_key: apiKey,
          ai_config_id: aiConfigId,
        }),
      )
    } catch (err: any) {
      setFailure(String(err?.message || err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <button
        type="button"
        onClick={run}
        disabled={busy}
        className="btn btn-secondary"
        style={{ alignSelf: 'flex-start', padding: compact ? '4px 8px' : '6px 10px', fontSize: '0.78rem' }}
      >
        <HeartPulse size={14} />
        <span>
          {busy
            ? t('ui_checking')
            : t('ui_check_key_endpoint')}
        </span>
      </button>
      {failure && <span style={{ fontSize: '0.75rem', color: '#f87171' }}>{failure}</span>}
      {view.text && (
        <span
          style={{
            fontSize: '0.75rem',
            color: view.ok ? '#34d399' : '#f87171',
            display: 'flex',
            gap: 6,
            flexWrap: 'wrap',
          }}
        >
          <span>
            {view.ok ? '✓' : '✕'} {view.text}
          </span>
          {view.detail && <span style={{ color: '#94a3b8', overflowWrap: 'anywhere' }}>{view.detail}</span>}
        </span>
      )}
    </div>
  )
}

function PromptEditorPanel({ prompts, aiConfigs, onSaved, onToast, lang }: {
  prompts: PromptTemplate[]
  aiConfigs: AIConfig[]
  onSaved: () => Promise<void>
  onToast: (msg: string) => void
  lang: UiLang
}) {
  const t = makeT(lang)
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
      onToast(t('ui_prompt_saved'))
      await onSaved()
      openEditor(null)
    } catch (e: any) {
      onToast(e.message || 'Error saving prompt')
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await api.deletePromptTemplate(id)
      onToast(t('ui_prompt_deleted'))
      await onSaved()
      if (selected?.id === id) openEditor(null)
    } catch (e: any) {
      onToast(e.message || 'Cannot delete system prompt')
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
      onToast(e.message || 'Test failed')
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="glass-panel" style={{ padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <h2 style={{ fontSize: '1.25rem', fontWeight: 800, display: 'flex', alignItems: 'center', gap: 10 }}>
            ✍️ {t('ui_rewrite_prompt_editor')}
          </h2>
          <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
            {t('ui_reusable_rewrite_prompts_for_forward_rules_system_prompts_read_o')}
          </p>
        </div>
        <button onClick={() => openEditor(null)} className="btn btn-primary" style={{ padding: '8px 14px' }}>
          + {t('ui_new_prompt')}
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
                title={t('ui_delete')}
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
                {t('ui_name')} *
              </label>
              <input className="input-field" style={{ width: '100%' }} value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={t('ui_e_g_formal_rewrite')} />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                {t('ui_output_language')}
              </label>
              <select className="input-field" style={{ width: '100%' }} value={form.target_language}
                onChange={(e) => setForm({ ...form, target_language: e.target.value })}>
                <option value="auto">{t('ui_auto_detect')}</option>
                {SUPPORTED_LANGS.filter((l) => l.code === 'fa' || l.code === 'en').map((l) => (
                  <option key={l.code} value={l.code}>{l.endonym}</option>
                ))}
              </select>
            </div>
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
              {t('ui_description')}
            </label>
            <input className="input-field" style={{ width: '100%' }} value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
              {t('ui_system_prompt')} *
              {selected?.is_system && <span style={{ color: '#f59e0b' }}> — {t('ui_system_read_only')}</span>}
            </label>
            <textarea
              className="input-field"
              style={{ width: '100%', minHeight: 110, resize: 'vertical', fontFamily: 'monospace', fontSize: '0.78rem' }}
              value={form.system_prompt}
              readOnly={!!selected?.is_system}
              onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
              placeholder={t('ui_rewrite_the_text_preserving_numbers_links_and_names_never_invent')}
            />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
              {t('ui_user_prompt_template')}
            </label>
            <input className="input-field" style={{ width: '100%', fontFamily: 'monospace', fontSize: '0.78rem' }}
              value={form.user_prompt_template} readOnly={!!selected?.is_system}
              onChange={(e) => setForm({ ...form, user_prompt_template: e.target.value })} placeholder="{text}" />
            <div style={{ fontSize: '0.7rem', color: '#64748b', marginTop: 4 }}>
              {t('ui_text_is_replaced_with_the_message_body')}
            </div>
          </div>

          {/* Test panel */}
          <div style={{ background: 'rgba(15, 23, 42, 0.6)', borderRadius: 10, padding: 14, border: '1px solid rgba(255,255,255,0.06)' }}>
            <div style={{ fontSize: '0.78rem', fontWeight: 700, color: '#a5b4fc', marginBottom: 8 }}>
              🧪 {t('ui_prompt_test_no_message_sent')}
            </div>
            <textarea className="input-field" style={{ width: '100%', minHeight: 60, marginBottom: 8, fontSize: '0.78rem' }}
              value={testText} onChange={(e) => setTestText(e.target.value)} />
            <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
              <button
                onClick={handleTest}
                disabled={testing || !selected || !aiConfigs.length}
                className="btn btn-secondary" style={{ padding: '6px 12px' }}
              >
                {testing ? '⏳' : '▶'} {t('ui_run_ai_test')}
              </button>
              {!aiConfigs.length && <span style={{ fontSize: '0.72rem', color: '#f87171' }}>
                {t('ui_create_an_ai_config_first')}
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
                💾 {saving ? (t('ui_app_saving')) : (t('ui_save_prompt'))}
              </button>
              {selected && (
                <button onClick={() => openEditor(null)} className="btn btn-secondary" style={{ padding: '8px 16px' }}>
                  {t('ui_cancel')}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
