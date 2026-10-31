import React, { lazy, Suspense, useCallback, useMemo, useState, useEffect, useRef } from 'react'
import {
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
  Check,
  ListTodo,
  ShieldAlert,
  Sliders,
  HeartPulse,
  Bot,
  Replace,
  Sun,
  Moon,
} from 'lucide-react'
import { api } from './api'
import { isAccepted, purgedCount, refusalReason } from './actionResult'
import { describeSaveFailure } from './providerKey'
import { isRtlLang, makeT, preferredLang, rememberLang, type UiLang } from './locales/engine'
import { LanguagePicker } from './components/LanguagePicker'
import type { ForwardRule, Session, AIConfig, AIProviderInfo, SaveAIConfigRequest, PromptTemplate, FilterRule, DeliveryJob, DeadLetterJob, DeliveryStats, RuleLiveStat, RecentError, GatewaySystemInfo, SimulateResponse, StatsResponse, LogItem, LogStats, ServicesHealthResponse, PVResponderConfig, DiagnosticsResponse, MeInfo } from './types'
import { getToken, applySession } from './session'
import { applyTheme, preferredTheme, toggleTheme, type Theme } from './theme'
import { PanelBoundary } from './components/PanelBoundary'
import { Navigation, NavTrigger, type NavItem, type TabId } from './components/Navigation'
import { ProviderKeyCheck, UserBar } from './components/ShellWidgets'
import { Toast } from './components/ui/Primitives'
import { ConfirmDialog } from './components/ConfirmDialog'
import { HealthChips } from './components/HealthChips'
import { AlertBanner, type PipelineAlert } from './components/AlertBanner'
import { ModalShell } from './components/ui/Modal'
import { Field } from './components/ui/Primitives'

// Every non-landing tab is a chunk. The shell ships with the landing panel
// and fetches the rest the first time they are opened.
const RulesPanel = lazy(() => import('./panels/RulesPanel'))
const SimulatorPanel = lazy(() => import('./panels/SimulatorPanel'))
const AiPanel = lazy(() => import('./panels/AiPanel'))
const PvPanel = lazy(() => import('./panels/PvPanel'))
const AutoReplyPanel = lazy(() => import('./panels/AutoReplyPanel'))
const ReplacePanel = lazy(() => import('./panels/ReplacePanel'))
const FiltersPanel = lazy(() => import('./panels/FiltersPanel'))
const SessionsPanel = lazy(() => import('./panels/SessionsPanel'))
const QueuePanel = lazy(() => import('./panels/QueuePanel'))
const DevopsPanel = lazy(() => import('./panels/DevopsPanel'))
const TaskOverviewModal = lazy(() =>
  import('./components/TaskOverviewModal').then((m) => ({ default: m.TaskOverviewModal })),
)
const RuleModal = lazy(() =>
  import('./components/RuleModal').then((m) => ({ default: m.RuleModal })),
)
const FilterModal = lazy(() =>
  import('./components/FilterModal').then((m) => ({ default: m.FilterModal })),
)
const AdminPanel = lazy(() => import('./components/AdminPanel').then((m) => ({ default: m.AdminPanel })))
const JobsPanel = lazy(() => import('./components/JobsPanel').then((m) => ({ default: m.JobsPanel })))
const AuditEventsPanel = lazy(() =>
  import('./components/AuditEventsPanel').then((m) => ({ default: m.AuditEventsPanel })),
)
const ClientSettings = lazy(() =>
  import('./components/ClientSettings').then((m) => ({ default: m.ClientSettings })),
)
const HomePanel = lazy(() => import('./panels/HomePanel').then((m) => ({ default: m.HomePanel })))

// ---------------------------------------------------------------------------
// Hash routing: the active tab lives in the URL (`#/rules`, `#/filters`, …).
//
// The tab used to be plain `useState`, so a refresh dropped the operator back
// on the landing tab and the mobile back button left the site instead of
// walking back through the tabs they had opened. Now:
//
// - the first render reads `location.hash` (a deep link opens the right tab);
// - switching tabs `pushState`s `#/<tab>`, so every tab is a history entry and
//   the browser Back button moves between tabs;
// - Back/Forward fire `hashchange`, and the listener below syncs the state —
//   it must write the state, not push again, or history would fork;
// - an unknown (or empty) hash resolves to the landing tab, and `#/admin` for
//   a non-admin is corrected once identity settles (the effect below).
// ---------------------------------------------------------------------------

const TAB_IDS: readonly TabId[] = [
  'system', 'rules', 'simulator', 'ai', 'pv', 'autoreply', 'replace', 'filters',
  'sessions', 'jobs', 'queue', 'devops', 'audit', 'admin', 'settings',
]

/** `#/rules` → `rules`; anything else — including no hash at all — → `system`. */
function tabFromHash(hash: string): TabId {
  const raw = hash.replace(/^#\/?/, '')
  return (TAB_IDS as readonly string[]).includes(raw) ? (raw as TabId) : 'system'
}

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
  // The tab is the URL: seeded from the hash so a refresh or a shared deep
  // link lands on the tab it names, and kept in sync with it below.
  const [activeTab, setActiveTabState] = useState<TabId>(() => tabFromHash(window.location.hash))

  /**
   * Tab switch with the URL kept in lockstep. `pushState` (not `replaceState`)
   * makes each tab a history entry, so Back walks tabs instead of leaving the
   * site; the `hashchange` listener below owns Back/Forward so this never
   * pushes twice for one navigation.
   */
  const changeTab = useCallback((tab: TabId) => {
    setActiveTabState(tab)
    if (tabFromHash(window.location.hash) !== tab) {
      window.history.pushState(null, '', `#/${tab}`)
    }
    // A tab switch that lands mid-page reads as broken — always start at the top.
    window.scrollTo({ top: 0 })
  }, [])

  // Back/Forward: the URL is the truth, the state follows it.
  useEffect(() => {
    const onHashChange = () => {
      setActiveTabState(tabFromHash(window.location.hash))
      window.scrollTo({ top: 0 })
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

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
  const [errorBanner, setErrorBanner] = useState<PipelineAlert | null>(null)
  // Feeds that failed to load on the last pass, surfaced in the error banner
  // with a Retry (the pipeline-error banner below reuses the same surface).
  const [loadFailures, setLoadFailures] = useState<string[] | null>(null)
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
  // Tenant scope of the log console. The API pins every non-admin to their own
  // tenant regardless; admins choose, and default to the installation-wide view.
  const [logScope, setLogScope] = useState<'mine' | 'all'>('all')
  // Once identity settles, everyone who is not an admin reads their own events
  // by default — matching the API's own default for them.
  useEffect(() => {
    if (currentUser && !currentUser.is_admin) setLogScope('mine')
  }, [currentUser])

  // The admin tab exists only for admins: a `#/admin` deep link opened by a
  // non-admin (stale bookmark, shared URL) would render an empty tab, so it is
  // corrected to the landing tab once identity settles.
  useEffect(() => {
    if (activeTab === 'admin' && currentUser && !currentUser.is_admin) changeTab('system')
  }, [activeTab, currentUser, changeTab])
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
  const [toast, setToast] = useState<{ message: string; tone: 'success' | 'danger' } | null>(null)
  const [toastClosing, setToastClosing] = useState(false)

  // Rule Modal — the draft lives inside <RuleModal />; the shell only tracks
  // which rule is open (null = create a new one from the clean default).
  const [isRuleModalOpen, setIsRuleModalOpen] = useState(false)
  const [ruleModalRule, setRuleModalRule] = useState<ForwardRule | null>(null)
  const [taskOverviewRule, setTaskOverviewRule] = useState<ForwardRule | null>(null)
  const [isTaskOverviewOpen, setIsTaskOverviewOpen] = useState(false)

  // AI Modal
  const [isAiModalOpen, setIsAiModalOpen] = useState(false)
  const [aiFormData, setAiFormData] = useState<Partial<SaveAIConfigRequest>>({})

  // Filter Modal — same split: the draft lives inside <FilterModal />.
  const [isFilterModalOpen, setIsFilterModalOpen] = useState(false)
  const [filterModalRule, setFilterModalRule] = useState<FilterRule | null>(null)

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
  // Memoized on the language so the callbacks below (and the memoized rule
  // cards that receive them) stay stable between renders of the same locale.
  const t = useMemo(() => makeT(lang), [lang])

  // Surface theme. index.html applies the stored value before the first paint;
  // this state mirrors it so the toggle icon (and its label) track reality, and
  // the effect re-asserts it for a document whose pre-paint script never ran.
  const [theme, setThemeState] = useState<Theme>(() => preferredTheme())
  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  // Toast lifecycle: visible 4s, then a short exit beat (toast--closing)
  // before the message drops, so it leaves with a transition instead of
  // vanishing. One ref-held timer chain — a toast shown mid-flight of
  // another cancels the older timers instead of being closed early by them.
  const toastTimers = useRef<Array<number>>([])
  const showToast = useCallback((msg: string, tone: 'success' | 'danger' = 'success') => {
    for (const id of toastTimers.current) window.clearTimeout(id)
    toastTimers.current = []
    setToast({ message: msg, tone })
    setToastClosing(false)
    toastTimers.current.push(
      window.setTimeout(() => {
        setToastClosing(true)
        toastTimers.current.push(
          window.setTimeout(() => {
            setToast(null)
            setToastClosing(false)
          }, 150),
        )
      }, 4000),
    )
  }, [])
  /** Failures read as failures: every catch path announces with the danger tone. */
  const showError = useCallback(
    (msg: string) => {
      showToast(msg, 'danger')
    },
    [showToast],
  )

  /** The one destructive-action confirm for shell-level flows: a real
   * alertdialog instead of window.confirm, which blocks the main thread and
   * ignores the Escape/focus contract the rest of the dashboard honors. The
   * caller keeps its flow; only the continuation past the gate moves into
   * `run`. */
  const [pendingConfirm, setPendingConfirm] = useState<{
    title: string
    body?: string
    run: () => Promise<void> | void
  } | null>(null)
  const [confirmBusy, setConfirmBusy] = useState(false)
  const requestConfirm = useCallback(
    (title: string, run: () => Promise<void> | void, body?: string) => {
      setPendingConfirm({ title, body, run })
    },
    [],
  )
  const runConfirmed = useCallback(async () => {
    const action = pendingConfirm
    if (!action) return
    setConfirmBusy(true)
    try {
      await action.run()
    } finally {
      setConfirmBusy(false)
      setPendingConfirm(null)
    }
  }, [pendingConfirm])

  const loadData = async () => {
    setLoading(true)
    // Every panel feed is independent, so one failed endpoint must not blank
    // the others. Failures are collected (not swallowed) and surfaced in the
    // dismissible banner with a Retry that re-runs this loader.
    const failures: string[] = []
    const track = <T,>(label: string, fallback: T) => (): T => {
      failures.push(label)
      return fallback
    }
    try {
      const [r, s, ai, pt, f, q, dlq, gw, st, l, ds, pv, prov] = await Promise.all([
        api.getRules().catch(track('rules', [] as ForwardRule[])),
        api.getSessions().catch(track('sessions', [] as Session[])),
        api.getAIConfigs().catch(track('ai', [] as AIConfig[])),
        api.getPromptTemplates().catch(track('prompts', [] as PromptTemplate[])),
        api.getFilters().catch(track('filters', [] as FilterRule[])),
        api.getQueueJobs().catch(track('queue', [] as DeliveryJob[])),
        api.getDLQJobs().catch(track('dlq', [] as DeadLetterJob[])),
        api.getGatewayInfo().catch(track('gateway', null)),
        api.getStats().catch(track('stats', null)),
        api.getLogs().catch(track('logs', [] as LogItem[])),
        api.getDeliveryStats().catch(track('delivery', null)),
        api.getPVResponder().catch(track('pv', null)),
        api.getAIProviders().catch(track('providers', [] as AIProviderInfo[])),
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
          if (!text) return null
          // The banner is structured now: severity variant, FloodWait
          // explanation with countdown, and the untouched diagnostic detail.
          // All three historical dismissal keys stay honoured.
          return {
            kind: (worst.severity || '').toLowerCase() === 'error' ? 'error' : 'warning',
            name,
            detail: detail || name,
            ts: worst.ts || 0,
            dismissKeys: [errKey, text, sliced],
          } as PipelineAlert
        })())
      }
      if (r.length > 0 && !simSelectedRuleId) {
        setSimSelectedRuleId(r[0].id)
      }
      setLoadFailures(failures.length ? failures : null)
    } catch (e: any) {
      showError(e.message || t('ui_error_loading_dashboard_data'))
    } finally {
      setLoading(false)
    }
  }

  /**
   * v18 log fetch: one page of history with the real match count. `offset`
   * walks backwards, so the console can reach the oldest record instead of
   * only ever showing the newest 100 rows.
   *
   * The poller re-arms on every query change, so two fetches can be in flight
   * at once; a generation counter makes the older response a no-op instead of
   * letting a stale page overwrite the newer one.
   */
  const logFetchGen = useRef(0)
  const fetchLogs = async (
    srv = logFilterService,
    lvl = logFilterLevel,
    srch = logSearch,
    opts?: { rule?: string; order?: 'desc' | 'asc'; offset?: number; limit?: number },
  ) => {
    const gen = ++logFetchGen.current
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
      // Tenant scope: 'mine' asks the API for the caller's own events
      // (`mine=1`); 'all' sends nothing, which the server reads as the default
      // (installation-wide for admins, own tenant for everyone else).
      if (logScope === 'mine') params.mine = true
      const [page, stats] = await Promise.all([
        api.getLogsPage(params).catch(() => null),
        api.getLogStats({
          ...(rule !== 'all' ? { rule_id: rule } : {}),
          ...(logScope === 'mine' ? { mine: true } : {}),
        }).catch(() => null),
      ])
      if (gen !== logFetchGen.current) return
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
      showError(e?.message || (t('ui_export_failed')))
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
    requestConfirm(
      t('ui_confirm_purge_logs', { scope: scope ? `${scope} · ` : '', window: ageWindow }),
      async () => {
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
          showError(e.message || t('ui_error_purging_logs'))
        } finally {
          setPurging(false)
        }
      },
    )
  }

  // Produce a migration bundle of everything this account owns. The bundle
  // contains live credentials; a passphrase encrypts it with AES-GCM.
  const handleBackupExport = async () => {
    if (backupBusy) return
    if (!backupPassphrase) {
      requestConfirm(t('ui_confirm_backup_without_passphrase'), () => runBackupExport())
      return
    }
    await runBackupExport()
  }

  const runBackupExport = async () => {
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
      showError(e.message || t('ui_error_creating_backup'))
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
    requestConfirm(t('ui_confirm_backup_restore', { verb, rows: rowsNote }), async () => {
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
        showError(e.message || t('ui_error_restoring_backup'))
      } finally {
        setBackupBusy(false)
      }
    })
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
    // The session is already verified at boot (Root); this read only fills in
    // the admin flag and identity. A failure leaves currentUser null — which
    // degrades admin-only surfaces — so it is logged, not silently dropped.
    api.me().then((info) => {
      setCurrentUser(info)
    }).catch((e) => {
      console.warn('profile load failed', e)
    })
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
    // from. The tenant scope is part of the query too.
  }, [activeTab, logAutoRefresh, logFilterService, logFilterLevel, logSearch, logFilterRule, logOrder, logOffset, logPageSize, logScope])

  // Rules Handlers — memoized so the memoized rule cards in RulesPanel do not
  // re-render every time the shell re-renders around them.
  const handleToggleRule = useCallback(
    async (id: string) => {
      try {
        const updated = await api.toggleRule(id)
        setRules((prev) => prev.map((r) => (r.id === id ? updated : r)))
        showToast(t('ui_rule_status_updated'))
      } catch (e: any) {
        showError(e.message)
      }
    },
    [showToast, t],
  )

  // Deletion is confirmed by the caller (a ConfirmDialog), never here.
  const handleDeleteRule = useCallback(
    async (id: string) => {
      try {
        await api.deleteRule(id)
        setRules((prev) => prev.filter((r) => r.id !== id))
        setIsTaskOverviewOpen(false)
        showToast(t('ui_rule_deleted_successfully'))
      } catch (e: any) {
        showError(e.message)
      }
    },
    [showToast, t],
  )

  const handleDuplicateRule = useCallback(
    async (id: string) => {
      try {
        const cloned = await api.duplicateRule(id)
        setRules((prev) => [cloned, ...prev])
        showToast(t('ui_task_duplicated_successfully'))
      } catch (e: any) {
        showError(e.message || (t('ui_failed_to_duplicate_task')))
      }
    },
    [showToast, t],
  )

  const handleSaveTaskOverview = useCallback(
    async (updated: ForwardRule) => {
      try {
        const res = await api.saveRule(updated as any)
        setRules((prev) => prev.map((r) => (r.id === res.id ? res : r)))
        showToast(t('ui_task_updated_successfully'))
      } catch (e: any) {
        showError(e.message || (t('ui_failed_to_save_task')))
      }
    },
    [showToast, t],
  )

  const handleQuickRouteSwitch = useCallback(
    async (ruleId: string, pathType: 'route1_direct' | 'route2_vip_hop' | 'route3_native') => {
      try {
        const updated = await api.quickSetRoutePath(ruleId, {
          path_type: pathType,
        })
        setRules((prev) => prev.map((r) => (r.id === ruleId ? updated : r)))
        showToast(t('ui_rule_route_switched_successfully'))
      } catch (e: any) {
        showError(e.message)
      }
    },
    [showToast, t],
  )

  const handlePauseRule = useCallback(
    async (id: string) => {
      try {
        await api.pauseRule(id)
        setRules((prev) => prev.map((r) => (r.id === id ? { ...r, is_paused: true } : r)))
        showToast(t('ui_rule_paused'))
      } catch (e: any) {
        showError(e.message)
      }
    },
    [showToast, t],
  )

  const handleResumeRule = useCallback(
    async (id: string) => {
      try {
        await api.resumeRule(id)
        setRules((prev) => prev.map((r) => (r.id === id ? { ...r, is_paused: false } : r)))
        showToast(t('ui_rule_resumed'))
      } catch (e: any) {
        showError(e.message)
      }
    },
    [showToast, t],
  )

  // The modal owns its draft; the shell only remembers which rule to seed it
  // with (null = a new task from the clean default, no demo data).
  const openRuleModal = useCallback((rule: ForwardRule | null) => {
    setRuleModalRule(rule)
    setIsRuleModalOpen(true)
  }, [])

  const handleSavedRule = useCallback(
    (saved: ForwardRule) => {
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
    },
    [showToast, t],
  )

  const openFilterModal = useCallback((filter: FilterRule | null) => {
    setFilterModalRule(filter)
    setIsFilterModalOpen(true)
  }, [])

  const handleSavedFilter = useCallback(
    (saved: FilterRule) => {
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
    },
    [showToast, t],
  )

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
      showError(e.message || t('ui_error_saving_pv_config'))
    } finally {
      setPvSaving(false)
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
    requestConfirm(t('ui_delete_this_ai_config'), async () => {
      try {
        await api.deleteAIConfig(id)
        setAiConfigs((prev) => prev.filter((a) => a.id !== id))
        showToast(t('ui_ai_config_deleted'))
      } catch (e: any) {
        showError(e.message)
      }
    })
  }

  // Filter Handlers — saving lives in <FilterModal />; the shell applies the
  // result. Deletion is confirmed by the panel's ConfirmDialog, never here.
  const handleDeleteFilter = async (id: string) => {
    try {
      await api.deleteFilter(id)
      setFilterRules((prev) => prev.filter((f) => f.id !== id))
      showToast(t('ui_filter_rule_deleted'))
    } catch (e: any) {
      showError(e.message)
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
      showError(e.message)
    }
  }

  const handlePurgeDLQ = async () => {
    requestConfirm(t('ui_purge_all_dlq_entries'), async () => {
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
        showError(e.message)
      }
    })
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
      showError(e.message)
    }
  }

  const handleTerminateSession = async (sess: Session) => {
    requestConfirm(
      t('ui_terminate_session_sess_2', { sess_2: sess.first_name || sess.username || sess.id }),
      async () => {
        try {
          const res = await api.terminateSession(sess.id)
          if (res.success) {
            setSessions((prev) => prev.filter((s) => s.id !== sess.id))
            showToast(t('ui_session_terminated'))
          } else {
            showToast(res.message || (t('ui_terminate_failed')))
          }
        } catch (e: any) {
          showError(e.message)
        }
      },
    )
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
      showError(e.message || t('ui_simulation_error'))
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
    setActiveTab: changeTab,
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
    loadFailures,
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
    logScope,
    setLogScope,
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
    toastClosing,
    setToast,
    isRuleModalOpen,
    setIsRuleModalOpen,
    ruleModalRule,
    openRuleModal,
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
    filterModalRule,
    openFilterModal,
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
    theme,
    setThemeState,
    showToast,
    showError,
    pendingConfirm,
    confirmBusy,
    runConfirmed,
    setPendingConfirm,
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
    handleSavePV,
    handleSavedRule,
    handleSaveAI,
    handleDeleteAI,
    handleSavedFilter,
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
    sessions,
    aiConfigs,
    aiProviders,
    promptTemplates,
    jobsPending,
    setJobsPending,
    errorBanner,
    setErrorBanner,
    setDismissedErrors,
    loadFailures,
    gatewayInfo,
    stats,
    loading,
    toast,
    toastClosing,
    isRuleModalOpen,
    setIsRuleModalOpen,
    ruleModalRule,
    handleSavedRule,
    taskOverviewRule,
    isTaskOverviewOpen,
    setIsTaskOverviewOpen,
    isAiModalOpen,
    setIsAiModalOpen,
    aiFormData,
    setAiFormData,
    isFilterModalOpen,
    setIsFilterModalOpen,
    filterModalRule,
    handleSavedFilter,
    isRtl,
    t,
    theme,
    setThemeState,
    loadData,
    handleDeleteRule,
    handleSaveTaskOverview,
    handleSaveAI,
    showToast,
    pendingConfirm,
    confirmBusy,
    runConfirmed,
    setPendingConfirm,
  } = d

  // Labels for the load-failure banner, resolved through the catalog so every
  // locale names the feed that failed.
  const loadFailureLabels = useMemo(() => {
    if (!loadFailures?.length) return []
    const labels: Record<string, string> = {
      rules: t('ui_rules_routes'),
      sessions: t('ui_telegram_sessions'),
      ai: t('ui_ai_intelligence'),
      prompts: t('ui_rewrite_prompt_editor'),
      filters: t('ui_advanced_filters'),
      queue: t('ui_queue_dlq'),
      dlq: t('ui_queue_dlq'),
      delivery: t('ui_queue_dlq'),
      gateway: t('ui_web_gateway'),
      stats: t('ui_overview_stats'),
      logs: t('ui_devops_logs'),
      pv: t('ui_pv_assistant'),
      providers: t('ui_ai_intelligence'),
    }
    return [...new Set(loadFailures.map((f) => labels[f] || f))]
  }, [loadFailures, t])

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
      { id: 'autoreply', label: t('ui_autoreply'), icon: <Bot size={18} /> },
      { id: 'replace', label: t('ui_replace'), icon: <Replace size={18} /> },
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
      {/* Load-failure banner — which feeds did not load, with a Retry that
          re-runs the loader. Failures used to be swallowed with `.catch(() => [])`,
          so a dead endpoint looked like an empty account. */}
      {loadFailureLabels.length > 0 && (
        <div className="load-failure-banner" role="alert">
          <span style={{ flexShrink: 0, lineHeight: 1.4 }} aria-hidden="true">⚠️</span>
          <span style={{ flex: 1, minWidth: 0, wordBreak: 'break-word' }}>
            {t('ui_data_load_failed')}
            {': '}
            <span style={{ fontWeight: 700 }}>{loadFailureLabels.join(' · ')}</span>
          </span>
          <button
            onClick={() => void loadData()}
            className="btn btn--ghost btn--sm"
            style={{ flexShrink: 0 }}
          >
            <RotateCcw size={13} aria-hidden="true" />
            <span>{t('ui_try_again')}</span>
          </button>
        </div>
      )}
      {/* Proactive notification — newest ERROR/WARN from the delivery pipeline,
          explained (FloodWait gets a countdown) with the diagnostic preserved. */}
      {errorBanner && (
        <AlertBanner
          alert={errorBanner}
          lang={lang}
          onDismiss={() => {
            setDismissedErrors((prev) => {
              const next = new Set(prev)
              for (const key of errorBanner.dismissKeys) next.add(key)
              try {
                sessionStorage.setItem('atf_dismissed_errors', JSON.stringify([...next]))
              } catch {}
              return next
            })
            setErrorBanner(null)
          }}
        />
      )}
      {/* One always-mounted live region: a message that appears later is still
          announced, and the region is never a surprise insertion. */}
      <div className="toast-region" role="status" aria-live="polite">
        {toast ? (
          <Toast message={toast.message} tone={toast.tone} closing={toastClosing} lang={lang} />
        ) : null}
      </div>

      {/* One shell-level destructive-action confirm: every requestConfirm()
          gate lands here instead of in a blocking window.confirm(). */}
      <ConfirmDialog
        open={!!pendingConfirm}
        lang={lang}
        title={pendingConfirm?.title || ''}
        body={pendingConfirm?.body}
        confirmLabel={t('ui_yes')}
        cancelLabel={t('ui_cancel')}
        busy={confirmBusy}
        onConfirm={runConfirmed}
        onCancel={() => setPendingConfirm(null)}
      />

      {/* Header Bar */}
      <header className="glass-panel app-header header-container">
        {/* Row 1: drawer trigger + product identity + the signed-in identity.
            One row on every viewport — the trigger never floats alone above
            the brand, and the user bar is not a separate card above it. */}
        <div className="header-top">
          {/* Drawer trigger: CSS hides it from --bp-md up, where the rail is docked. */}
          <NavTrigger
            open={navOpen}
            onOpenChange={setNavOpen}
            lang={lang}
          />
          <div className="header-title-wrap">
            <div className="brand-mark" aria-hidden="true">
              <Radio size={22} />
            </div>
            <div className="header-titles">
              <div className="header-title-row">
                <h1 className="header-title">
                  AutoTelegramForward <span className="header-title__accent">PRO GATEWAY</span>
                </h1>
                <span className="badge badge-vip header-version">
                  {gatewayInfo?.version ? `v${gatewayInfo.version} RELEASE` : t('ui_version_probing')}
                </span>
              </div>
              <p className="text-balance header-subtitle">
                {t('ui_unified_smart_telegram_forwarding_gateway_microservice_platform')}
              </p>
            </div>
          </div>
          <UserBar lang={lang} user={currentUser} />
        </div>

        {/* Row 2: service health (one scrollable strip) + workspace tools.
            `undefined` means "not probed yet", never painted as an outage. */}
        <div className="header-actions">
          <div className="header-chips" role="status">
            <HealthChips
              lang={lang}
              core={stats?.atf_core_online}
              logger={stats?.atf_logger_online}
              web={gatewayInfo?.atf_web_online}
            />
          </div>

          <div className="header-tools">
            {/* Language picker: all ten catalogs, by endonym */}
            <LanguagePicker lang={lang} onChange={setLang} compact />

            {/* Theme: the surface the operator works on, not a preference buried
                in settings. Dark is the product's identity; light is first-class
                (token swap on <html data-theme>, no per-panel code). */}
            <button
              type="button"
              onClick={() => setThemeState((prev) => toggleTheme(prev))}
              className="btn btn--ghost btn--icon btn--sm theme-toggle"
              aria-label={theme === 'dark' ? t('ui_theme_switch_to_light') : t('ui_theme_switch_to_dark')}
              title={theme === 'dark' ? t('ui_theme_switch_to_light') : t('ui_theme_switch_to_dark')}
            >
              {theme === 'dark' ? <Sun size={16} aria-hidden="true" /> : <Moon size={16} aria-hidden="true" />}
            </button>

            {/* Reload: refreshes every panel feed. */}
            <button
              type="button"
              onClick={loadData}
              disabled={loading}
              className="btn btn--ghost btn--icon btn--sm"
              aria-label={t('ui_refresh')}
              title={t('ui_refresh')}
            >
              <RotateCcw size={14} aria-hidden="true" />
            </button>
          </div>
        </div>
      </header>

      {/* Overview stat cards now live on the Home tab (HomePanel) — other tabs
          get the whole width for their own content. */}

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
      {/* TAB 0: HOME — greeting, quick actions, KPIs, readiness, activity.
          The raw readiness dump (SystemPanel) stays reachable from it. */}
      {activeTab === 'system' && (
        <PanelBoundary lang={lang}>
          <HomePanel d={d} />
        </PanelBoundary>
      )}

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

      {/* TAB: AI AUTO-REPLY (v28 subsystem, per-tenant) */}
      {
        activeTab === 'autoreply' && (
          <PanelBoundary lang={lang}>
            <AutoReplyPanel d={d} />
          </PanelBoundary>
        )
      }

      {/* TAB: TEXT REPLACEMENT (one manager, shared with the bot and mobile) */}
      {
        activeTab === 'replace' && (
          <PanelBoundary lang={lang}>
            <ReplacePanel d={d} />
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

      {/* TAB 8: MY JOBS — boundary-wrapped like every other lazy chunk, so a
          failed panel chunk degrades to the panel error state instead of
          blanking through the root boundary. */}
      {activeTab === 'jobs' && (
        <PanelBoundary lang={lang}>
          <JobsPanel
            lang={lang}
            onPendingChange={setJobsPending}
          />
        </PanelBoundary>
      )}

      {activeTab === 'audit' && (
        <PanelBoundary lang={lang}>
          <AuditEventsPanel lang={lang} />
        </PanelBoundary>
      )}

      {/* TAB 9: ADMIN */}
      {activeTab === 'admin' && currentUser?.is_admin && (
        <PanelBoundary lang={lang}>
          <AdminPanel lang={lang} />
        </PanelBoundary>
      )}

      {/* TAB 9: CLIENT SETTINGS */}
      {activeTab === 'settings' && (
        <PanelBoundary lang={lang}>
          <ClientSettings lang={lang} />
        </PanelBoundary>
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

      {/* CREATE / EDIT RULE MODAL — extracted; the draft lives inside it */}
      <Suspense fallback={null}>
        <RuleModal
          open={isRuleModalOpen}
          lang={lang}
          sessions={sessions}
          aiConfigs={aiConfigs}
          promptTemplates={promptTemplates}
          rule={ruleModalRule}
          onClose={() => setIsRuleModalOpen(false)}
          onSaved={handleSavedRule}
          showToast={showToast}
        />
      </Suspense>

      {/* CREATE / EDIT AI MODAL — the shared ModalShell owns overlay, Escape,
          focus trap and scroll lock; this body is only the form. */}
      {isAiModalOpen && (() => {
        // The catalogue is the core's list; this only reads it. `provider`
        // arrives from the API as the enum name (OPENAI) and the catalogue is
        // keyed by id (openai), so compare case-insensitively.
        const providerId = (aiFormData.provider || 'openai').toLowerCase()
        const providerMeta = aiProviders.find((p) => p.id === providerId)
        const endpointHint = providerMeta?.default_base_url || ''
        const modelIdeas = providerMeta?.models || []
        return (
        <ModalShell
          open
          lang={lang}
          titleId="ai-config-modal-title"
          title={t('ui_ai_engine_configuration')}
          width={580}
          onClose={() => setIsAiModalOpen(false)}
        >
          <form onSubmit={handleSaveAI} className="ai-form">
            <Field label={`${t('ui_configuration_name')} *`} htmlFor="ai-config-name">
              <input
                id="ai-config-name"
                type="text"
                required
                value={aiFormData.name || ''}
                onChange={(e) => setAiFormData({ ...aiFormData, name: e.target.value })}
                className="input-field"
                placeholder={t('ui_openai_gpt_4o_fast')}
              />
            </Field>

            <div className="ai-form__grid">
              <Field label={t('ui_provider')} htmlFor="ai-config-provider">
                <select
                  id="ai-config-provider"
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
              </Field>
              <Field label={t('ui_model_name')} htmlFor="ai-config-model">
                <input
                  id="ai-config-model"
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
              </Field>
            </div>

            <Field
              label={t('ui_provider_endpoint_base_url')}
              htmlFor="ai-config-base-url"
              hint={endpointHint
                ? t('ui_leave_blank_to_use_this_provider_s_default_endpointhint_2', { endpointHint_2: endpointHint })
                : t('ui_for_a_custom_provider_enter_the_full_address_of_your_own_endpoin')}
            >
              <input
                id="ai-config-base-url"
                type="text"
                value={aiFormData.base_url || ''}
                onChange={(e) => setAiFormData({ ...aiFormData, base_url: e.target.value })}
                className="input-field"
                dir="ltr"
                placeholder={endpointHint || 'https://api.example.com/v1'}
              />
            </Field>

            <Field
              label={t('ui_app_api_key')}
              htmlFor="ai-config-api-key"
              hint={aiFormData.id
                ? t('ui_leave_blank_to_keep_the_stored_key')
                : t('ui_stored_on_the_server_only_it_is_never_sent_back_to_this_page')}
            >
              <input
                id="ai-config-api-key"
                type="password"
                value={aiFormData.api_key || ''}
                onChange={(e) => setAiFormData({ ...aiFormData, api_key: e.target.value })}
                className="input-field"
                dir="ltr"
                placeholder="sk-..."
              />
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
            </Field>

            <Field label={t('ui_system_prompt')} htmlFor="ai-config-system-prompt">
              <textarea
                id="ai-config-system-prompt"
                rows={3}
                value={aiFormData.system_prompt || ''}
                onChange={(e) => setAiFormData({ ...aiFormData, system_prompt: e.target.value })}
                className="input-field"
                placeholder={t('ui_you_are_an_expert_forex_signal_translator')}
              />
            </Field>

            <div className="ai-form__actions">
              <button type="button" onClick={() => setIsAiModalOpen(false)} className="btn btn-secondary">
                {t('ui_cancel')}
              </button>
              <button type="submit" className="btn btn-primary">
                <Check size={16} aria-hidden="true" />
                <span>{t('ui_save')}</span>
              </button>
            </div>
          </form>
        </ModalShell>
        )
      })()}

      {/* CREATE / EDIT FILTER MODAL — extracted; the draft lives inside it */}
      <Suspense fallback={null}>
        <FilterModal
          open={isFilterModalOpen}
          lang={lang}
          filter={filterModalRule}
          onClose={() => setIsFilterModalOpen(false)}
          onSaved={handleSavedFilter}
          showToast={showToast}
        />
      </Suspense>

    </div>
  )
}
export default App

