import React, { lazy, useMemo, useState, useEffect } from 'react'
import {
  Send,
  Radio,
  PlayCircle,
  Users,
  RotateCcw,
  Layers,
  Shield,
  Sparkles,
  Filter,
  Server,
  Activity,
  X,
  Check,
  ListTodo,
  ShieldAlert,
  Sliders,
  HeartPulse,
} from 'lucide-react'
import { api } from './api'
import { isAccepted, purgedCount, refusalReason } from './actionResult'
import { describeSaveFailure } from './providerKey'
import { isRtlLang, makeT, preferredLang, rememberLang, type UiLang } from './locales/engine'
import { LanguagePicker } from './components/LanguagePicker'
import type { ForwardRule, SaveRuleRequest, Session, AIConfig, AIProviderInfo, SaveAIConfigRequest, PromptTemplate, FilterRule, SaveFilterRuleRequest, DeliveryJob, DeadLetterJob, DeliveryStats, RuleLiveStat, RecentError, GatewaySystemInfo, SimulateResponse, StatsResponse, LogItem, LogStats, ServicesHealthResponse, PVResponderConfig, DiagnosticsResponse, MeInfo } from './types'
import { getToken, applySession } from './session'
import { PanelBoundary } from './components/PanelBoundary'
import { Navigation, NavTrigger, type NavItem } from './components/Navigation'
import { ProviderKeyCheck, UserBar } from './components/ShellWidgets'
import { Toast } from './components/ui/Primitives'
import { BRANDING_HOP_KEY, brandingHopSummary, brandingListToText, brandingTextToList, readBrandingHop, type BrandingHopConfig } from './dashboard/branding'

// Every non-landing tab is a chunk. The shell ships with the landing panel
// and fetches the rest the first time they are opened.
const RulesPanel = lazy(() => import('./panels/RulesPanel'))
const SimulatorPanel = lazy(() => import('./panels/SimulatorPanel'))
const AiPanel = lazy(() => import('./panels/AiPanel'))
const PvPanel = lazy(() => import('./panels/PvPanel'))
const FiltersPanel = lazy(() => import('./panels/FiltersPanel'))
const SessionsPanel = lazy(() => import('./panels/SessionsPanel'))
const QueuePanel = lazy(() => import('./panels/QueuePanel'))
const DevopsPanel = lazy(() => import('./panels/DevopsPanel'))
const TaskOverviewModal = lazy(() =>
  import('./components/TaskOverviewModal').then((m) => ({ default: m.TaskOverviewModal })),
)
const AdminPanel = lazy(() => import('./components/AdminPanel').then((m) => ({ default: m.AdminPanel })))
const JobsPanel = lazy(() => import('./components/JobsPanel').then((m) => ({ default: m.JobsPanel })))
const AuditEventsPanel = lazy(() =>
  import('./components/AuditEventsPanel').then((m) => ({ default: m.AuditEventsPanel })),
)
const ClientSettings = lazy(() =>
  import('./components/ClientSettings').then((m) => ({ default: m.ClientSettings })),
)
const SystemPanel = lazy(() => import('./components/SystemPanel').then((m) => ({ default: m.SystemPanel })))

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


/**
 * All dashboard state, loaders and mutators, in one hook.
 *
 * The component used to hold this inline next to ~4 000 lines of JSX, so none
 * of it could be memoised, tested or reviewed on its own. State here, markup in
 * `App` and in the lazy panels under `panels/`: a panel receives one typed
 * context instead of forty positional props.
 *
 * `DashboardCtx` is `ReturnType<typeof useDashboard>`, so a binding that leaves
 * this hook breaks every panel that reads it at compile time.
 */
function useDashboard(me?: MeInfo) {

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
      showToast(e.message || t('ui_error_loading_dashboard_data'))
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
      showToast(e.message || t('ui_error_saving_pv_config'))
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
      let msg = e.message || t('ui_error')
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
      showToast(e.message || t('ui_simulation_error'))
    } finally {
      setSimulating(false)
    }
  }

  return {
    rules,
    sessions,
    filterRules,
    pvConfig,
    lang,
    setLang,
    activeTab,
    setActiveTab,
    currentUser,
    setCurrentUser,
    setRules,
    setSessions,
    aiConfigs,
    setAiConfigs,
    aiProviders,
    setAiProviders,
    setFilterRules,
    promptTemplates,
    setPromptTemplates,
    queueJobs,
    setQueueJobs,
    dlqJobs,
    setDlqJobs,
    deliveryStats,
    setDeliveryStats,
    ruleLiveStats,
    setRuleLiveStats,
    recentErrors,
    setRecentErrors,
    diagnostics,
    setDiagnostics,
    jobsPending,
    setJobsPending,
    diagLoading,
    setDiagLoading,
    refreshDiagnostics,
    errorBanner,
    setErrorBanner,
    dismissedErrors,
    setDismissedErrors,
    gatewayInfo,
    setGatewayInfo,
    stats,
    setStats,
    logs,
    setLogs,
    logFilterService,
    setLogFilterService,
    logFilterLevel,
    setLogFilterLevel,
    logSearch,
    setLogSearch,
    logAutoRefresh,
    setLogAutoRefresh,
    logStats,
    setLogStats,
    logFilterRule,
    setLogFilterRule,
    logOrder,
    setLogOrder,
    logPageSize,
    setLogPageSize,
    logOffset,
    setLogOffset,
    logTotal,
    setLogTotal,
    logHasMore,
    setLogHasMore,
    logExporting,
    setLogExporting,
    purging,
    setPurging,
    purgeWindowH,
    setPurgeWindowH,
    purgeResult,
    setPurgeResult,
    backupBusy,
    setBackupBusy,
    backupPassphrase,
    setBackupPassphrase,
    backupCounts,
    setBackupCounts,
    backupEncrypted,
    setBackupEncrypted,
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
    setServicesHealth,
    loading,
    setLoading,
    toast,
    setToast,
    isRuleModalOpen,
    setIsRuleModalOpen,
    ruleFormData,
    setRuleFormData,
    ruleMeta,
    setRuleMeta,
    taskOverviewRule,
    setTaskOverviewRule,
    isTaskOverviewOpen,
    setIsTaskOverviewOpen,
    isAiModalOpen,
    setIsAiModalOpen,
    aiFormData,
    setAiFormData,
    isFilterModalOpen,
    setIsFilterModalOpen,
    filterFormData,
    setFilterFormData,
    simText,
    setSimText,
    simOriginChatId,
    setSimOriginChatId,
    simOriginTitle,
    setSimOriginTitle,
    simOriginUser,
    setSimOriginUser,
    simHasMedia,
    setSimHasMedia,
    simIsProtected,
    setSimIsProtected,
    simSelectedRuleId,
    setSimSelectedRuleId,
    simResult,
    setSimResult,
    simulating,
    setSimulating,
    setPvConfig,
    pvSaving,
    setPvSaving,
    pvPreset,
    setPvPreset,
    isRtl,
    t,
    showToast,
    loadData,
    fetchLogs,
    handleExportLogs,
    handlePurgeLogs,
    handleBackupExport,
    handleBackupImport,
    onBackupFileChosen,
    handleToggleRule,
    handleDeleteRule,
    handleDuplicateRule,
    handleSaveTaskOverview,
    handleQuickRouteSwitch,
    brandingHop,
    patchBrandingHop,
    brandingListDrafts,
    setBrandingListDrafts,
    brandingDraftKey,
    brandingListValue,
    onBrandingListInput,
    handleSavePV,
    handleSaveRule,
    handleSaveAI,
    handleDeleteAI,
    handleSaveFilter,
    handleDeleteFilter,
    handleRetryDLQ,
    handlePurgeDLQ,
    handlePauseRule,
    handleResumeRule,
    handleBackupSession,
    handleTerminateSession,
    handleRunSimulation,
  }
}

/** Everything a dashboard panel may read or call. */
export type DashboardCtx = ReturnType<typeof useDashboard>

export function App({ me }: { me?: MeInfo } = {}) {
  const d = useDashboard(me)
  const {
    lang,
    setLang,
    activeTab,
    setActiveTab,
    currentUser,
    aiConfigs,
    aiProviders,
    promptTemplates,
    dlqJobs,
    jobsPending,
    setJobsPending,
    errorBanner,
    setErrorBanner,
    setDismissedErrors,
    gatewayInfo,
    stats,
    loading,
    toast,
    isRuleModalOpen,
    setIsRuleModalOpen,
    ruleFormData,
    setRuleFormData,
    ruleMeta,
    setRuleMeta,
    taskOverviewRule,
    isTaskOverviewOpen,
    setIsTaskOverviewOpen,
    isAiModalOpen,
    setIsAiModalOpen,
    aiFormData,
    setAiFormData,
    isFilterModalOpen,
    setIsFilterModalOpen,
    filterFormData,
    setFilterFormData,
    isRtl,
    t,
    loadData,
    handleDeleteRule,
    handleSaveTaskOverview,
    brandingHop,
    patchBrandingHop,
    brandingListValue,
    onBrandingListInput,
    handleSaveRule,
    handleSaveAI,
    handleSaveFilter,
  } = d

  // The navigation is data, not markup: one list feeds the docked rail and the
  // drawer, so a tab cannot exist in one and be missing from the other.
  const [navOpen, setNavOpen] = useState(false)
  const navItems: NavItem[] = useMemo(() => {
    const items: NavItem[] = [
      { id: 'system', label: t('ui_system'), icon: <Activity size={18} /> },
      { id: 'rules', label: t('ui_rules_routes'), icon: <Layers size={18} /> },
      { id: 'simulator', label: t('ui_smart_simulator'), icon: <PlayCircle size={18} /> },
      { id: 'ai', label: t('ui_ai_intelligence'), icon: <Sparkles size={18} /> },
      { id: 'pv', label: t('ui_pv_assistant'), icon: <HeartPulse size={18} /> },
      { id: 'filters', label: t('ui_advanced_filters'), icon: <Filter size={18} /> },
      { id: 'sessions', label: t('ui_telegram_sessions'), icon: <Users size={18} /> },
      { id: 'jobs', label: t('ui_my_jobs'), icon: <ListTodo size={18} />, badge: jobsPending },
      { id: 'queue', label: t('ui_queue_dlq'), icon: <Activity size={18} /> },
      { id: 'devops', label: t('ui_devops_logs'), icon: <Server size={18} /> },
      { id: 'audit', label: t('ui_audit_events'), icon: <Shield size={18} /> },
      { id: 'settings', label: t('ui_client_settings'), icon: <Sliders size={18} /> },
    ]
    if (currentUser?.is_admin) {
      items.push({ id: 'admin', label: t('ui_master_admin'), icon: <ShieldAlert size={18} /> })
    }
    return items
  }, [lang, currentUser?.is_admin, jobsPending])

  return (
    <div dir={isRtl ? 'rtl' : 'ltr'} className="app-shell">
      <a className="skip-link" href="#main-content">
        {t('ui_skip_to_content')}
      </a>
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
      {/* One always-mounted live region: a message that appears later is still
          announced, and the region is never a surprise insertion. */}
      <div className="toast-region" role="status" aria-live="polite">
        {toast ? <Toast message={toast} tone="success" lang={lang} /> : null}
      </div>

      {/* Header Bar */}
      <header
        className="glass-panel app-header header-container"
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
        {/* Drawer trigger: CSS hides it from --bp-md up, where the rail is docked. */}
        <NavTrigger
          open={navOpen}
          onOpenChange={setNavOpen}
          lang={lang}
        />
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
              <span className="badge badge-vip" style={{ fontSize: 'var(--type-xs)', padding: '2px 8px' }}>
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
            <div style={{ fontSize: 'var(--type-xs)', color: '#94a3b8' }}>{t('ui_active_rules')}</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 800 }}>
              {stats?.active_rules ?? 0} <span style={{ fontSize: '0.8rem', color: 'var(--color-text-muted)' }}>/ {stats?.total_rules ?? 0}</span>
            </div>
          </div>
        </div>

        <div className="glass-panel overview-card" style={{ padding: '14px 16px', display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ padding: 8, borderRadius: 10, background: 'rgba(245, 158, 11, 0.15)', color: '#fbbf24', flexShrink: 0 }}>
            <Send size={20} />
          </div>
          <div>
            <div style={{ fontSize: 'var(--type-xs)', color: '#94a3b8' }}>{t('ui_forwarded_24h')}</div>
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
            <div style={{ fontSize: 'var(--type-xs)', color: '#94a3b8' }}>{t('ui_connected_sessions')}</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 800 }}>
              {stats?.active_sessions ?? 0} <span style={{ fontSize: '0.8rem', color: 'var(--color-text-muted)' }}>/ {stats?.total_sessions ?? 0}</span>
            </div>
          </div>
        </div>

        <div className="glass-panel overview-card" style={{ padding: '14px 16px', display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ padding: 8, borderRadius: 10, background: 'rgba(239, 68, 68, 0.15)', color: '#f87171', flexShrink: 0 }}>
            <Activity size={20} />
          </div>
          <div>
            <div style={{ fontSize: 'var(--type-xs)', color: '#94a3b8' }}>{t('ui_dead_letter_queue')}</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 800, color: dlqJobs.length > 0 ? '#ef4444' : '#94a3b8' }}>
              {dlqJobs.length}
            </div>
          </div>
        </div>
      </div>

      {/* Body: docked rail (>= 1024px) or drawer (< 1024px), then the panels. */}
      <div className="app-body">
        <Navigation
          items={navItems}
          active={activeTab}
          onSelect={setActiveTab}
          open={navOpen}
          onOpenChange={setNavOpen}
          lang={lang}
        />
        <main id="main-content" className="app-main" tabIndex={-1}>
      {/* TAB 0: SYSTEM — readiness, topology, contract */}
      {activeTab === 'system' && <SystemPanel lang={lang} />}

      {/* TAB 1: RULES & ROUTES */}
      {
        activeTab === 'rules' && (
          <PanelBoundary lang={lang}>
            <RulesPanel d={d} />
          </PanelBoundary>
        )
      }

      {/* TAB 2: SMART SIMULATOR */}
      {
        activeTab === 'simulator' && (
          <PanelBoundary lang={lang}>
            <SimulatorPanel d={d} />
          </PanelBoundary>
        )
      }

      {/* TAB 3: AI INTELLIGENCE */}
      {
        activeTab === 'ai' && (
          <PanelBoundary lang={lang}>
            <AiPanel d={d} />
          </PanelBoundary>
        )
      }

      {/* PROMPT EDITOR & TESTER (AI Rewrite) */}
      

      {/* TAB: AI PV ASSISTANT */}
      {
        activeTab === 'pv' && (
          <PanelBoundary lang={lang}>
            <PvPanel d={d} />
          </PanelBoundary>
        )
      }

      {/* TAB 4: ADVANCED FILTERS */}
      {
        activeTab === 'filters' && (
          <PanelBoundary lang={lang}>
            <FiltersPanel d={d} />
          </PanelBoundary>
        )
      }

      {/* TAB 5: SESSIONS */}
      {
        activeTab === 'sessions' && (
          <PanelBoundary lang={lang}>
            <SessionsPanel d={d} />
          </PanelBoundary>
        )
      }

      {/* TAB 6: QUEUE & DLQ */}
      {
        activeTab === 'queue' && (
          <PanelBoundary lang={lang}>
            <QueuePanel d={d} />
          </PanelBoundary>
        )
      }

      {/* TAB 7: DEVOPS GATEWAY & PRO ACTIVE LOGGER */}
      {
        activeTab === 'devops' && (
          <PanelBoundary lang={lang}>
            <DevopsPanel d={d} />
          </PanelBoundary>
        )
      }

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

        </main>
      </div>

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
                              <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginTop: 4 }}>
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
                              placeholder={t('ui_example_keywords')}
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
                <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginTop: 8 }}>
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
                          { id: 'custom', label: t('ui_custom_openai_compatible'), default_base_url: '', models: [] },
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
                <span style={{ display: 'block', fontSize: 'var(--type-xs)', color: '#94a3b8', marginTop: 4 }}>
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
                <span style={{ display: 'block', fontSize: 'var(--type-xs)', color: '#94a3b8', marginTop: 4 }}>
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
export default App

