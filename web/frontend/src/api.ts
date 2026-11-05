import type {
  AIConfig,
  TaskKind,
  UserTask,
  UserTaskPage,
  AIProviderInfo,
  AuthResponse,
  BotTokenInfo,
  BackupExportRequest,
  BackupExportResponse,
  BackupImportRequest,
  BackupImportResponse,
  DeadLetterJob,
  DeliveryJob,
  DeliveryStats,
  DiagnosticEventEntry,
  DiagnosticsResponse,
  EnvContract,
  FilterRule,
  ForwardRule,
  GatewaySystemInfo,
  LogItem,
  LogPage,
  LogPolicyResponse,
  LogPolicyUpdateRequest,
  LogPolicyUpdateResponse,
  LogQuery,
  LogStats,
  LoginAccountResponse,
  PVResponderConfig,
  PromptTemplate,
  PurgeLogsRequest,
  PurgeLogsResponse,
  QueryAuditEventsParams,
  QueryAuditEventsResponse,
  RecentError,
  RegisterAccountResponse,
  RoutePathQuickSetRequest,
  RuleLiveStat,
  RuleSettingsResponse,
  ChatListPage,
  CheckProviderKeyRequest,
  CheckProviderKeyResponse,
  SaveAIConfigRequest,
  SaveFilterRuleRequest,
  SavePromptTemplateRequest,
  SaveRuleRequest,
  AutoReplySettings,
  AutoReplyPrompt,
  AutoReplyPromptBundle,
  AutoReplyTarget,
  AutoReplyRule,
  AutoReplyJob,
  AutoReplyJobPage,
  AutoReplyStats,
  AutoReplyPreviewResponse,
  SaveAutoReplyPromptRequest,
  SaveAutoReplyRuleRequest,
  ReplaceRule,
  ReplaceRuleInput,
  ReplaceRulePage,
  ReplaceRuleTestResult,
  FilterTestResult,
  ReplaceTaskOption,
  ServicesHealthResponse,
  Session,
  SetupReport,
  SimulateRequest,
  SimulateResponse,
  StatsResponse,
  TestPromptRequest,
  TestPromptResponse,
} from './types'
import { clearSession, getToken } from './session'
import type {
  WorkerErrorQuery,
  WorkerErrorsResponse,
  WorkerControlOpResponse,
  WorkerControlStatusResponse,
  WorkerIncidentsResponse,
  WorkerQueueResponse,
  WorkerStatusResponse,
  WorkerTasksResponse,
} from './workerOps'

const API_BASE = '/api'

/** Bearer header for raw fetches (downloads, blobs) outside fetchJson. */
function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}

}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string>),
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  const res = await fetch(url, {
    ...init,
    headers,
  })
  if (res.status === 401 && !url.includes('/api/auth/')) {
    // ONE session transition per burst of 401s: clearSession is idempotent
    // (local storage remove + the same event), and auth_required is a
    // level signal — the one listener reads it as "go to the login screen",
    // so a 5-request burst landing at once still renders exactly one prompt.
    clearSession()
    window.dispatchEvent(new Event('auth_required'))
    throw new Error('Authentication required')
  }
  if (!res.ok) {
    // Normalize once, at the boundary: every caller's catch — and therefore
    // every toast — sees the server's own sentence ("{"error":"..."}" /
    // "{"message":"..."}"), never a raw JSON blob or an HTML error page.
    throw new Error(await errorMessageOf(res))
  }
  return res.json()
}

/** Pull the server's own message out of an error response. */
async function errorMessageOf(res: Response): Promise<string> {
  const fallback = `HTTP ${res.status}`
  let raw = ''
  try {
    raw = (await res.text()).trim()
  } catch {
    return fallback
  }
  if (!raw) return fallback
  const parsed = safeJsonParse(raw)
  if (parsed && typeof parsed === 'object') {
    for (const key of ['error', 'message', 'detail']) {
      const value = (parsed as Record<string, unknown>)[key]
      if (typeof value === 'string' && value.trim()) return value
    }
  }
  // Not JSON (or JSON without a message field): show the body only when it is
  // short, plain text — never dump a multi-KB HTML page into a toast.
  if (raw.length <= 200 && !raw.startsWith('<')) return raw
  return `${fallback}`
}

function safeJsonParse(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

// Shared single fetch of /queue so the shell's loaders (queue jobs, delivery
// stats, live per-rule counters, recent errors) don't issue several redundant
// network calls on every refresh cycle. TTL matches the shell's poll cadence.
let _queueCache: { data: any; ts: number } | null = null
const QUEUE_TTL = 4000
async function getQueueRaw(): Promise<any> {
  const now = Date.now()
  if (_queueCache && now - _queueCache.ts < QUEUE_TTL) {
    return _queueCache.data
  }
  const res = await fetchJson<any>(`${API_BASE}/queue`)
  _queueCache = { data: res, ts: Date.now() }
  return res
}

/** Server rows may omit fields (older cores, partial projections); a
 * normalized entry keeps the timeline rendering defensive without lying. */
function normalizeDiagnosticEvent(e: any): DiagnosticEventEntry {
  return {
    event_id: e?.event_id ?? '',
    event_name: e?.event_name ?? '',
    timestamp: typeof e?.timestamp === 'number' ? e.timestamp : 0,
    severity: e?.severity ?? 'INFO',
    stage: e?.stage ?? '',
    strategy: e?.strategy ?? '',
    attempt: typeof e?.attempt === 'number' ? e.attempt : 0,
    task_id: e?.task_id ?? '',
    job_id: e?.job_id ?? '',
    trace_id: e?.trace_id ?? '',
    correlation_id: e?.correlation_id ?? '',
    source_peer_id: e?.source_peer_id ?? '',
    source_message_id: typeof e?.source_message_id === 'number' ? e.source_message_id : 0,
    target_peer_id: e?.target_peer_id ?? '',
    error_origin: e?.error_origin ?? '',
    category: e?.category ?? '',
    reason: e?.reason ?? '',
    retryable: e?.retryable === true,
    detail: typeof e?.detail === 'string' ? e.detail : undefined,
  }
}

export const api = {
  getStats: () => fetchJson<StatsResponse>(`${API_BASE}/stats`),
  // Readiness surface (v19). Shared verbatim with the CLI wizard and the mobile
  // client so all three surfaces report the same verdict for the same server.
  getSystemSetup: () => fetchJson<SetupReport>(`${API_BASE}/system/setup`),
  getSystemEnv: () => fetchJson<EnvContract>(`${API_BASE}/system/env`),
  getGatewayInfo: () => fetchJson<GatewaySystemInfo>(`${API_BASE}/gateway`),

  // Forward Rules
  getRules: async (): Promise<ForwardRule[]> => {
    const res = await fetchJson<any>(`${API_BASE}/rules`)
    if (Array.isArray(res)) return res
    if (Array.isArray(res?.rules)) return res.rules
    return []
  },
  getRule: (id: string) => fetchJson<ForwardRule>(`${API_BASE}/rules/${id}`),
  saveRule: (req: SaveRuleRequest) =>
    fetchJson<ForwardRule>(
      req.id
        ? `${API_BASE}/rules/${encodeURIComponent(req.id)}`
        : `${API_BASE}/rules`,
      {
        method: req.id ? 'PUT' : 'POST',
        body: JSON.stringify(req),
      },
    ),
  // Per-task settings: the full control surface the Telegram bot edits.
  // The schema endpoint needs no rule, so an editor can be built before the
  // user owns anything.
  getRuleSettingsSchema: () =>
    fetchJson<RuleSettingsResponse>(`${API_BASE}/rules/settings/schema`),
  getRuleSettings: (id: string) =>
    fetchJson<RuleSettingsResponse>(`${API_BASE}/rules/${encodeURIComponent(id)}/settings`),
  updateRuleSettings: (
    id: string,
    settings: Record<string, any>,
    replaceAll = false,
    /**
     * Settings to hand back to the bot's default (or the registry's, if the bot
     * has none). This is the only way to say "stop overriding this": writing the
     * default value instead would leave the key pinned to it, and the task would
     * stop following the bot.
     */
    reset: string[] = [],
  ) =>
    fetchJson<RuleSettingsResponse>(
      `${API_BASE}/rules/${encodeURIComponent(id)}/settings`,
      {
        method: 'PATCH',
        body: JSON.stringify({
          settings,
          replace_all: replaceAll,
          ...(reset.length ? { reset } : {}),
        }),
      },
    ),
  // Bot-route credentials. The bot token travels one way: it goes up in the
  // request body and never comes back — every answer carries id + @username.
  listBotTokens: async (): Promise<BotTokenInfo[]> => {
    const res = await fetchJson<any>(`${API_BASE}/bot-tokens`)
    if (Array.isArray(res)) return res
    if (Array.isArray(res?.tokens)) return res.tokens
    return []
  },
  // Bot-token errors are normalized by fetchJson itself, so the server's own
  // reason reaches the panel verbatim without a per-call wrapper.
  createBotToken: async (token: string, label = ''): Promise<BotTokenInfo> => {
    const res = await fetchJson<any>(`${API_BASE}/bot-tokens`, {
      method: 'POST',
      body: JSON.stringify({ token, label }),
    })
    return res?.token ?? res
  },
  deleteBotToken: (id: string): Promise<void> =>
    fetchJson<void>(`${API_BASE}/bot-tokens/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  // Partial update: only the named fields are applied, and a named field is
  // applied even when its value is zero/false — the only way to clear a value.
  patchRule: (id: string, patch: Record<string, any>, updateMask: string[]) =>
    fetchJson<ForwardRule>(`${API_BASE}/rules/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ ...patch, update_mask: updateMask }),
    }),
  deleteRule: (id: string) =>
    fetchJson<{ success: boolean; id: string }>(`${API_BASE}/rules/${id}`, {
      method: 'DELETE',
    }),
  toggleRule: (id: string) =>
    fetchJson<ForwardRule>(`${API_BASE}/rules/${id}/toggle`, {
      method: 'POST',
    }),
  quickSetRoutePath: (id: string, req: RoutePathQuickSetRequest) =>
    fetchJson<ForwardRule>(`${API_BASE}/rules/${id}/route-path`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  pauseRule: (id: string, until?: number) =>
    fetchJson<{ success: boolean; message?: string }>(`${API_BASE}/rules/${id}/pause`, {
      method: 'POST',
      body: JSON.stringify(until ? { until } : {}),
    }),
  resumeRule: (id: string) =>
    fetchJson<{ success: boolean; message?: string }>(`${API_BASE}/rules/${id}/resume`, {
      method: 'POST',
    }),
  duplicateRule: (id: string) =>
    fetchJson<ForwardRule>(`${API_BASE}/rules/${id}/duplicate`, {
      method: 'POST',
    }),

  // Sessions
  getSessions: async (): Promise<Session[]> => {
    const res = await fetchJson<any>(`${API_BASE}/sessions`)
    if (Array.isArray(res)) return res
    if (Array.isArray(res?.sessions)) return res.sessions
    return []
  },
  backupSession: (sessionId: string) =>
    fetchJson<{ success: boolean; encrypted_session_data?: string; message?: string }>(`${API_BASE}/sessions/${sessionId}/backup`, {
      method: 'POST',
    }),
  terminateSession: (sessionId: string) =>
    fetchJson<{ success: boolean; message?: string }>(`${API_BASE}/sessions/${sessionId}`, {
      method: 'DELETE',
    }),
  /**
   * Browse one session's dialogs — the source/target picker feed.
   *
   * A session that exists but has no live Telegram client answers 503; the
   * picker treats that as "manual entry only" rather than an empty account.
   * Older servers may not have the route at all (404), which the picker shows
   * as unavailable, again without blocking the id field.
   */
  listChats: (
    sessionId: string,
    params: { query?: string; limit?: number; offset?: number } = {},
  ): Promise<ChatListPage> => {
    const q = new URLSearchParams()
    if (params.query) q.set('query', params.query)
    if (params.limit) q.set('limit', String(params.limit))
    if (params.offset) q.set('offset', String(params.offset))
    const qs = q.toString()
    return fetchJson<ChatListPage>(
      `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/chats${qs ? `?${qs}` : ''}`,
    )
  },

  // AI Configurations
  getAIConfigs: async (): Promise<AIConfig[]> => {
    const res = await fetchJson<any>(`${API_BASE}/ai-configs`)
    if (Array.isArray(res)) return res
    if (Array.isArray(res?.configs)) return res.configs
    return []
  },
  saveAIConfig: (req: SaveAIConfigRequest) =>
    fetchJson<AIConfig>(
      (req as any).id
        ? `${API_BASE}/ai-configs/${encodeURIComponent((req as any).id)}`
        : `${API_BASE}/ai-configs`,
      {
        method: (req as any).id ? 'PUT' : 'POST',
        body: JSON.stringify(req),
      },
    ),
  // What the core can call, with the endpoint each provider defaults to.
  getAIProviders: async (): Promise<AIProviderInfo[]> => {
    const res = await fetchJson<any>(`${API_BASE}/ai-configs/providers`)
    if (Array.isArray(res?.providers)) return res.providers
    return []
  },
  // Ask the provider before the key is saved: the answer is what makes an
  // endpoint addable instead of a guess.
  checkProviderKey: (req: CheckProviderKeyRequest) =>
    fetchJson<CheckProviderKeyResponse>(`${API_BASE}/ai-configs/check-key`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  deleteAIConfig: (id: string) =>
    fetchJson<{ success: boolean; id: string }>(`${API_BASE}/ai-configs/${id}`, {
      method: 'DELETE',
    }),

  // AI Rewrite prompt templates (auth-scoped: /api/prompts, not /api/v1/*)
  getPromptTemplates: async (): Promise<PromptTemplate[]> => {
    const res = await fetchJson<any>(`${API_BASE}/prompts`)
    if (Array.isArray(res?.templates)) return res.templates
    return []
  },
  savePromptTemplate: (req: SavePromptTemplateRequest & { current_version?: number }) =>
    fetchJson<PromptTemplate>(
      req.id
        ? `${API_BASE}/prompts/${encodeURIComponent(req.id)}`
        : `${API_BASE}/prompts`,
      {
        method: req.id ? 'PUT' : 'POST',
        body: JSON.stringify({
          template: req,
          expected_version: (req as any).current_version || 0,
        }),
      },
    ),
  deletePromptTemplate: (id: string) =>
    fetchJson<{ status: string }>(`${API_BASE}/prompts/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  testPrompt: (req: TestPromptRequest) =>
    fetchJson<TestPromptResponse>(`${API_BASE}/prompts/test`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),

  // Filter Rules
  getFilters: async (): Promise<FilterRule[]> => {
    const res = await fetchJson<any>(`${API_BASE}/filters`)
    if (Array.isArray(res)) return res
    if (Array.isArray(res?.filters)) return res.filters
    return []
  },
  // v30: one stored filter by id. The server 404s a filter the caller does not
  // own, so the client never learns whether somebody else's filter exists.
  getFilter: (id: string) =>
    fetchJson<FilterRule>(`${API_BASE}/filters/${encodeURIComponent(id)}`),
  saveFilter: (req: SaveFilterRuleRequest) =>
    fetchJson<FilterRule>(
      (req as any).id
        ? `${API_BASE}/filters/${encodeURIComponent((req as any).id)}`
        : `${API_BASE}/filters`,
      {
        method: (req as any).id ? 'PUT' : 'POST',
        body: JSON.stringify(req),
      },
    ),
  // v30: flip only the enable/disable switch server-side. The server flips the
  // value it currently stores, so a stale tab cannot overwrite lists another
  // device just saved.
  toggleFilter: (id: string) =>
    fetchJson<FilterRule>(`${API_BASE}/filters/${encodeURIComponent(id)}/toggle`, {
      method: 'POST',
    }),
  deleteFilter: (id: string) =>
    fetchJson<{ success: boolean; id: string }>(`${API_BASE}/filters/${id}`, {
      method: 'DELETE',
    }),
  /**
   * Preview a content filter against a sample message.
   *
   * Read-only by contract on the server (Core `preview_match`): nothing is
   * persisted and nothing is forwarded. The draft fields are merged over the
   * stored rule identified by `ruleId`, so an editor can test an unsaved
   * change without a save round-trip.
   */
  testFilterRule: (
    ruleId: string,
    sample: string,
    draft: {
      name?: string
      whitelist_keywords?: string[]
      blacklist_keywords?: string[]
      regex_patterns?: string[]
      regex_block?: string[]
      regex_require?: string[]
    } = {},
  ) =>
    fetchJson<FilterTestResult>(`${API_BASE}/filters/test`, {
      method: 'POST',
      body: JSON.stringify({ rule_id: ruleId, sample, ...draft }),
    }),

  // Queue & DLQ
  getQueueJobs: async (): Promise<DeliveryJob[]> => {
    const res = await getQueueRaw()
    if (Array.isArray(res)) return res
    if (Array.isArray(res?.jobs)) return res.jobs
    if (Array.isArray(res?.rules)) {
      return res.rules.map((r: any) => ({
        id: r.rule_id || '',
        rule_id: r.rule_id || '',
        source_chat_id: '',
        source_message_id: r.in_queue || 0,
        target_chat_id: '',
        status: (r.in_queue || 0) > 0 ? 'QUEUED' : 'IDLE',
        attempts: 0,
        max_attempts: 3,
        delivery_stage: `In Queue: ${r.in_queue || 0}`,
        created_at: Math.floor(Date.now() / 1000),
      }))
    }
    return []
  },
  getDeliveryStats: async (): Promise<DeliveryStats | null> => {
    try {
      const res = await getQueueRaw()
      if (res && typeof res === 'object' && !Array.isArray(res)) {
        return {
          processed_total: res.processed_total ?? res.forwarded_total ?? 0,
          forwarded_total: res.forwarded_total ?? 0,
          failed_total: res.failed_total ?? 0,
          dedup_skipped_total: res.dedup_skipped_total ?? 0,
          filtered_total: res.filtered_total ?? 0,
          in_queue: res.in_queue ?? 0,
          dead_lettered_total: res.dead_lettered_total ?? 0,
          retry_total: res.retry_total ?? 0,
          rules: Array.isArray(res.rules) ? res.rules : [],
          errors: Array.isArray(res.errors) ? res.errors : [],
        }
      }
      return null
    } catch {
      return null
    }
  },
  getRuleLiveStats: async (): Promise<RuleLiveStat[]> => {
    try {
      const res = await getQueueRaw()
      if (Array.isArray(res?.rules)) {
        return res.rules.map((r: any) => ({
          rule_id: r.rule_id || r.id || '',
          rule_name: r.rule_name || r.name || '',
          is_active: !!r.is_active,
          is_paused: !!r.is_paused,
          forwarded: r.forwarded ?? 0,
          filtered: r.filtered ?? 0,
          errors: r.errors ?? 0,
          last_forward_ts: r.last_forward_ts ?? 0,
          last_error: r.last_error ?? '',
        }))
      }
      return []
    } catch {
      // Live counters are decorative next to the rule list; a failed read must
      // not break the panel that hosts them.
      return []
    }
  },
  getRecentErrors: async (): Promise<RecentError[]> => {
    try {
      const res = await getQueueRaw()
      if (Array.isArray(res?.errors)) {
        return res.errors.map((e: any) => ({
          ts: e.ts ?? 0,
          rule_id: e.rule_id ?? '',
          category: e.category ?? '',
          error_name: e.error_name ?? e.category ?? '',
          severity: e.severity ?? 'error',
          detail: e.detail ?? '',
          chat_id: e.chat_id ?? '',
        }))
      }
      return []
    } catch {
      return []
    }
  },
  getDLQJobs: async (): Promise<DeadLetterJob[]> => {
    const res = await fetchJson<any>(`${API_BASE}/dlq`)
    if (Array.isArray(res)) return res
    if (Array.isArray(res?.jobs)) return res.jobs
    if (Array.isArray(res?.entries)) return res.entries
    return []
  },
  // The core answers a replay with the full RuleActionResponse: success plus a
  // message/error_code pairing from the documented result codes
  // (requeued / not_found / already_pending / already_sent / replay_failed).
  // Typing it as the whole body keeps the caller honest about refusals.
  retryDLQ: (id: string) =>
    fetchJson<{ success: boolean; message: string; error_code?: string }>(`${API_BASE}/dlq/${id}/retry`, {
      method: 'POST',
    }),
  purgeDLQ: () =>
    fetchJson<{ success: boolean; message: string; purged_count?: number }>(`${API_BASE}/dlq`, {
      method: 'DELETE',
    }),

  // Diagnostic events: the worker's persisted per-message processing history.
  // The queue answers "how many", these answer "what exactly happened" — the
  // timeline drill-down groups one message's events oldest-first with every
  // Telegram answer preserved, which is what makes a failed delivery
  // explainable instead of just visible.
  getDiagnosticEvents: async (params?: {
    since?: number
    until?: number
    category?: string
    stage?: string
    severity?: string
    task_id?: string
    source_peer_id?: string
    source_message_id?: number
    trace_id?: string
    reason?: string
    limit?: number
  }): Promise<DiagnosticEventEntry[]> => {
    const q = new URLSearchParams()
    if (params?.since) q.set('since', String(params.since))
    if (params?.until) q.set('until', String(params.until))
    if (params?.category) q.set('category', params.category)
    if (params?.stage) q.set('stage', params.stage)
    if (params?.severity) q.set('severity', params.severity)
    if (params?.task_id) q.set('task_id', params.task_id)
    if (params?.source_peer_id) q.set('source_peer_id', params.source_peer_id)
    if (params?.source_message_id) q.set('source_message_id', String(params.source_message_id))
    if (params?.trace_id) q.set('trace_id', params.trace_id)
    if (params?.reason) q.set('reason', params.reason)
    if (params?.limit) q.set('limit', String(params.limit))
    const res = await fetchJson<any>(
      `${API_BASE}/diagnostic-events${q.toString() ? '?' + q.toString() : ''}`,
    )
    const events = Array.isArray(res?.events) ? res.events : []
    return events.map(normalizeDiagnosticEvent)
  },
  getMessageTimeline: async (
    sourcePeerId: string,
    sourceMessageId: number,
  ): Promise<DiagnosticEventEntry[]> => {
    const q = new URLSearchParams()
    q.set('source_peer_id', sourcePeerId)
    q.set('source_message_id', String(sourceMessageId))
    const res = await fetchJson<any>(`${API_BASE}/diagnostic-events/timeline?${q.toString()}`)
    const events = Array.isArray(res?.events) ? res.events : []
    return events.map(normalizeDiagnosticEvent)
  },
  getDiagnosticFacets: async (
    facet: 'category' | 'stage' | 'severity' | 'reason' | 'strategy' | 'error_origin',
    since?: number,
  ): Promise<{ facet: string; counts: { value: string; count: number }[] }> => {
    const q = new URLSearchParams()
    q.set('facet', facet)
    if (since) q.set('since', String(since))
    const res = await fetchJson<any>(`${API_BASE}/diagnostic-events/facets?${q.toString()}`)
    const counts = Array.isArray(res?.counts) ? res.counts : []
    return {
      facet: res?.facet ?? facet,
      counts: counts.map((c: any) => ({ value: c?.value ?? '', count: c?.count ?? 0 })),
    }
  },

  // Logging-pipeline health: whether log collection itself is working. An
  // empty log console must be distinguishable from a dead pipeline.
  getLogsHealth: () =>
    fetchJson<{ status?: string; healthy?: boolean; detail?: string; [key: string]: unknown }>(
      `${API_BASE}/logs/health`,
    ),

  // Bot-token defaults: the inheritance layer between task pins and registry
  // defaults. Reading and writing it is the only way a UI can show WHERE a
  // task's effective value came from — and clear named keys (absent `defaults`
  // + `clear` names) without pinning anything.
  getBotTokenDefaults: (id: string) =>
    fetchJson<{
      success: boolean
      message?: string
      error_code?: string
      defaults?: Record<string, unknown>
      total?: number
      changed?: number
    }>(`${API_BASE}/bot-tokens/${encodeURIComponent(id)}/defaults`),
  setBotTokenDefaults: (
    id: string,
    body: { defaults?: Record<string, unknown>; replace_all?: boolean; clear?: string[] },
  ) =>
    fetchJson<{
      success: boolean
      message?: string
      error_code?: string
      defaults?: Record<string, unknown>
      total?: number
      changed?: number
    }>(`${API_BASE}/bot-tokens/${encodeURIComponent(id)}/defaults`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  // Simulator & Logs
  simulate: (req: SimulateRequest) =>
    fetchJson<SimulateResponse>(`${API_BASE}/simulate`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  getLogs: async (params?: { service?: string; level?: string; search?: string; limit?: number }): Promise<LogItem[]> => {
    const page = await api.getLogsPage(params)
    return page.logs
  },
  /**
   * v18: the paging form of the log query. Returns the real match count and
   * has_more alongside this page, which is what makes deep history reachable
   * instead of "the newest N rows and nothing older".
   */
  getLogsPage: async (params?: LogQuery): Promise<LogPage> => {
    const q = new URLSearchParams()
    if (params?.service && params.service !== 'all') q.set('service', params.service)
    if (params?.level && params.level !== 'all') q.set('level', params.level)
    if (params?.levels?.length) params.levels.forEach((l) => q.append('levels', l))
    if (params?.category) q.set('category', params.category)
    if (params?.search) q.set('search', params.search)
    if (params?.rule_id) q.set('rule_id', params.rule_id)
    if (params?.correlation_id) q.set('correlation_id', params.correlation_id)
    if (params?.user_id) q.set('user_id', String(params.user_id))
    if (params?.mine) q.set('mine', '1')
    if (params?.since) q.set('since', String(params.since))
    if (params?.until) q.set('until', String(params.until))
    if (params?.hours) q.set('hours', String(params.hours))
    if (params?.offset) q.set('offset', String(params.offset))
    if (params?.page) q.set('page', String(params.page))
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.ascending) q.set('order', 'asc')
    const res = await fetchJson<any>(`${API_BASE}/logs${q.toString() ? '?' + q.toString() : ''}`)
    const logs: LogItem[] = Array.isArray(res) ? res : Array.isArray(res?.logs) ? res.logs : []
    const limit = res?.limit ?? params?.limit ?? 100
    return {
      logs,
      total: typeof res?.total === 'number' ? res.total : logs.length,
      count: typeof res?.count === 'number' ? res.count : logs.length,
      offset: typeof res?.offset === 'number' ? res.offset : params?.offset ?? 0,
      limit,
      has_more: typeof res?.has_more === 'boolean' ? res.has_more : logs.length >= limit,
    }
  },
  /** v18: one task's complete history, ownership-checked server-side. */
  getRuleLogs: async (ruleId: string, params?: LogQuery): Promise<LogPage> => {
    const q = new URLSearchParams()
    if (params?.level && params.level !== 'all') q.set('level', params.level)
    if (params?.levels?.length) params.levels.forEach((l) => q.append('levels', l))
    if (params?.search) q.set('search', params.search)
    if (params?.hours) q.set('hours', String(params.hours))
    if (params?.since) q.set('since', String(params.since))
    if (params?.until) q.set('until', String(params.until))
    if (params?.offset) q.set('offset', String(params.offset))
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.ascending) q.set('order', 'asc')
    const res = await fetchJson<any>(
      `${API_BASE}/rules/${encodeURIComponent(ruleId)}/logs${q.toString() ? '?' + q.toString() : ''}`,
    )
    const logs: LogItem[] = Array.isArray(res?.logs) ? res.logs : []
    const limit = res?.limit ?? params?.limit ?? 100
    return {
      logs,
      total: typeof res?.total === 'number' ? res.total : logs.length,
      count: logs.length,
      offset: typeof res?.offset === 'number' ? res.offset : params?.offset ?? 0,
      limit,
      has_more: typeof res?.has_more === 'boolean' ? res.has_more : logs.length >= limit,
      rule_id: res?.rule_id,
    }
  },
  /** v18: download the filtered history (json | csv) with the auth header. */
  exportLogs: async (params?: LogQuery & { format?: 'json' | 'csv' }): Promise<void> => {
    const q = new URLSearchParams()
    if (params?.service && params.service !== 'all') q.set('service', params.service)
    if (params?.level && params.level !== 'all') q.set('level', params.level)
    if (params?.levels?.length) params.levels.forEach((l) => q.append('levels', l))
    if (params?.category) q.set('category', params.category)
    if (params?.search) q.set('search', params.search)
    if (params?.rule_id) q.set('rule_id', params.rule_id)
    if (params?.correlation_id) q.set('correlation_id', params.correlation_id)
    if (params?.since) q.set('since', String(params.since))
    if (params?.until) q.set('until', String(params.until))
    if (params?.hours) q.set('hours', String(params.hours))
    q.set('format', params?.format || 'json')
    const res = await fetch(`${API_BASE}/logs/export?${q.toString()}`, { headers: authHeaders() })
    if (!res.ok) throw new Error(`Export failed (${res.status})`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `atf-logs-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.${params?.format || 'json'}`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  },
  getLogStats: async (params?: { rule_id?: string; mine?: boolean }): Promise<LogStats> => {
    const q = new URLSearchParams()
    if (params?.rule_id) q.set('rule_id', params.rule_id)
    if (params?.mine) q.set('mine', '1')
    return fetchJson<LogStats>(`${API_BASE}/logs/stats${q.toString() ? '?' + q.toString() : ''}`)
  },
  purgeLogs: (req: PurgeLogsRequest) =>
    fetchJson<PurgeLogsResponse>(`${API_BASE}/logs/purge`, {
      method: 'POST',      body: JSON.stringify(req),
    }),
  /** The collection policy each producer is enforcing, read from each process. */
  getLogPolicy: () => fetchJson<LogPolicyResponse>(`${API_BASE}/logs/policy`),
  /**
   * Change the policy atf-core enforces (admin-only). The response carries the
   * state read back from the core, not an echo of the request.
   */
  updateLogPolicy: (req: LogPolicyUpdateRequest) =>
    fetchJson<LogPolicyUpdateResponse>(`${API_BASE}/logs/policy`, {
      method: 'PUT',
      body: JSON.stringify(req),
    }),
  // Centralized audit events (observability platform) — tenant-scoped server-side.
  queryAuditEvents: async (params?: QueryAuditEventsParams): Promise<QueryAuditEventsResponse> => {
    const q = new URLSearchParams()
    if (params?.correlation_id) q.set('correlation_id', params.correlation_id)
    if (params?.operation_id) q.set('operation_id', params.operation_id)
    if (params?.feature_id) q.set('feature_id', params.feature_id)
    if (params?.service) q.set('service', params.service)
    if (params?.severity) q.set('severity', params.severity)
    if (params?.event_category) q.set('event_category', params.event_category)
    if (params?.outcome) q.set('outcome', params.outcome)
    if (params?.limit) q.set('limit', String(params.limit))
    if (params?.offset) q.set('offset', String(params.offset))
    const url = `${API_BASE}/audit/events${q.toString() ? '?' + q.toString() : ''}`
    const res = await fetchJson<any>(url)
    if (res?.events) return res
    return { events: [], total: 0 }
  },
  getServicesHealth: async (): Promise<ServicesHealthResponse> => {
    return fetchJson<ServicesHealthResponse>(`${API_BASE}/v1/health/services`)
  },

  // ------------------------------------------------- Authentication & Identity
  register: (req: { username: string; password: string; display_name?: string }) =>
    fetchJson<RegisterAccountResponse>(`${API_BASE}/auth/register`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  login: (req: { username: string; password: string }) =>
    fetchJson<LoginAccountResponse>(`${API_BASE}/auth/login`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  exchangeBotToken: (req: { login_code: string }) =>
    fetchJson<AuthResponse>(`${API_BASE}/auth/exchange-bot-token`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  authTMA: (initData: string) =>
    fetchJson<AuthResponse>(`${API_BASE}/auth/tma`, {
      method: 'POST',
      body: JSON.stringify({ init_data: initData }),
    }),
  me: () => fetchJson<{ user_id: number; username: string; is_admin: boolean; now: string }>(`${API_BASE}/auth/me`),

  // ------------------------------------------------- PV Responder (AI Assistant)
  getPVResponder: () => fetchJson<PVResponderConfig>(`${API_BASE}/pv-responder`),
  savePVResponder: (req: PVResponderConfig) =>
    fetchJson<PVResponderConfig>(`${API_BASE}/pv-responder`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),

  // ------------------------------------------------- Logout
  logout: () => {
    clearSession()
  },

  // ------------------------------------------------- Backup & Restore
  // Exports a JSON snapshot (live credentials inside — optionally encrypted
  // with a passphrase) so the install can be moved to another server.
  backupExport: (req: BackupExportRequest) =>
    fetchJson<BackupExportResponse>(`${API_BASE}/backup/export`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  backupImport: (req: BackupImportRequest) =>
    fetchJson<BackupImportResponse>(`${API_BASE}/backup/import`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),

  // ------------------------------------------------- Admin Control Plane
  adminGetOverview: () => fetchJson<any>(`${API_BASE}/admin/overview`),

  // Go Worker operational surface — the worker is the single telemetry
  // owner; the API proxies its /ops snapshot. Unreachable => 503 with
  // worker_reachable=false, which the UI must render as "worker offline"
  // and NEVER as an idle, healthy zeroed dashboard.
  workerStatus: () => fetchJson<WorkerStatusResponse>(`${API_BASE}/admin/worker/status`),
  workerQueue: () => fetchJson<WorkerQueueResponse>(`${API_BASE}/admin/worker/queue`),
  workerTasks: () => fetchJson<WorkerTasksResponse>(`${API_BASE}/admin/worker/tasks`),
  workerErrors: (params?: WorkerErrorQuery) => {
    const q = new URLSearchParams()
    if (params?.severity) q.set('severity', params.severity)
    if (params?.component) q.set('component', params.component)
    if (params?.rule_id) q.set('rule_id', params.rule_id)
    if (params?.since) q.set('since', String(params.since))
    if (params?.limit) q.set('limit', String(params.limit))
    const s = q.toString()
    return fetchJson<WorkerErrorsResponse>(`${API_BASE}/admin/worker/errors${s ? '?' + s : ''}`)
  },
  // Incidents (§9): repeated failures grouped into a lifecycle with a
  // suspected pattern + hint. state is validated at the API edge.
  workerIncidents: (state?: string) => {
    const q = state ? `?state=${encodeURIComponent(state)}` : ''
    return fetchJson<WorkerIncidentsResponse>(`${API_BASE}/admin/worker/incidents${q}`)
  },
  workerIncidentAck: (id: string) =>
    fetchJson<{ acknowledged: string }>(`${API_BASE}/admin/worker/incidents/ack?id=${encodeURIComponent(id)}`, {
      method: 'POST',
    }),
  // §10 worker controls. Pause/resume are POST + UI-confirmed — they mutate
  // live processing state.
  workerControlStatus: () =>
    fetchJson<WorkerControlStatusResponse>(`${API_BASE}/admin/worker/control/status`),
  workerControlPause: () =>
    fetchJson<WorkerControlOpResponse>(`${API_BASE}/admin/worker/control/pause`, { method: 'POST' }),
  workerControlResume: () =>
    fetchJson<WorkerControlOpResponse>(`${API_BASE}/admin/worker/control/resume`, { method: 'POST' }),

  adminListUsers: (limit = 50, offset = 0) =>
    fetchJson<{ users: any[]; total: number }>(`${API_BASE}/admin/users?limit=${limit}&offset=${offset}`),
  adminSetUserStatus: (id: number | string, status: string) =>
    fetchJson<{ success: boolean; message: string }>(`${API_BASE}/admin/users/${id}/status`, {
      method: 'POST',
      body: JSON.stringify({ status }),
    }),
  adminBroadcast: (message: string, target = 'all') =>
    fetchJson<{ success: boolean; broadcast_id: string; total_recipients: number; message: string }>(
      `${API_BASE}/admin/broadcast`,
      {
        method: 'POST',
        body: JSON.stringify({ message, target }),
      },
    ),
  adminGetSettings: () => fetchJson<any>(`${API_BASE}/admin/settings`),
  adminSaveSettings: (settings: {
    force_sub_enabled: boolean
    force_sub_channel: string
    web_url: string
    registration_open: boolean
  }) =>
    fetchJson<{ success: boolean; message: string }>(`${API_BASE}/admin/settings`, {
      method: 'POST',
      body: JSON.stringify(settings),
    }),
  adminListAdmins: () => fetchJson<{ admins: any[] }>(`${API_BASE}/admin/admins`),
  adminAddAdmin: (userId: number) =>
    fetchJson<{ success: boolean; message: string }>(`${API_BASE}/admin/admins`, {
      method: 'POST',
      body: JSON.stringify({ user_id: userId }),
    }),
  adminRemoveAdmin: (userId: number | string) =>
    fetchJson<{ success: boolean; message: string }>(`${API_BASE}/admin/admins/${userId}`, {
      method: 'DELETE',
    }),
  adminListExtensions: () => fetchJson<{ extensions: any[] }>(`${API_BASE}/admin/extensions`),

  // ------------------------------------------------- Per-user task lifecycle
  // Tenant identity travels as the bearer token only; no owner field is ever
  // sent (the server derives it from the verified JWT and ignores any that
  // appear in a body).
  listTasks: (limit = 50, offset = 0) =>
    fetchJson<UserTaskPage>(`${API_BASE}/tasks?limit=${limit}&offset=${offset}`),
  getTask: (taskId: string) =>
    fetchJson<{ task: UserTask }>(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`),
  createTask: (kind: TaskKind, payload: Record<string, any>, idempotencyKey?: string) =>
    fetchJson<{ task: UserTask; already_existed: boolean; id: string; status: string }>(
      `${API_BASE}/tasks`,
      {
        method: 'POST',
        body: JSON.stringify({
          kind,
          // The task store dedupes on (owner, kind, key) — a browser retry of
          // the SAME submission must never double-submit.
          idempotency_key: idempotencyKey ?? `web-${crypto.randomUUID()}`,
          ...(payload ? { payload } : {}),
        }),
      },
    ),
  cancelTask: (taskId: string) =>
    fetchJson<{ task: UserTask }>(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }),
  retryTask: (taskId: string) =>
    fetchJson<{ task: UserTask }>(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST' }),


  // --------------------------------------------------- AI auto-reply (v28)
  // Tenant identity is the bearer token only. The Go layer derives the owner
  // from the verified JWT and the core refuses every unscoped read, so no
  // owner id is ever sent or accepted here.
  getAutoReplySettings: () =>
    fetchJson<{ settings: AutoReplySettings }>(`${API_BASE}/auto-reply/settings`),
  updateAutoReplySettings: (req: Partial<AutoReplySettings> & { update_mask?: string[] }) =>
    fetchJson<{ settings: AutoReplySettings }>(`${API_BASE}/auto-reply/settings`, {
      method: 'PUT',
      body: JSON.stringify(req),
    }),
  listAutoReplyPrompts: (search = '') =>
    fetchJson<{ prompts: AutoReplyPrompt[]; total: number }>(
      `${API_BASE}/auto-reply/prompts${search ? `?search=${encodeURIComponent(search)}` : ''}`,
    ),
  saveAutoReplyPrompt: (req: SaveAutoReplyPromptRequest & { id?: string }) =>
    fetchJson<{ prompt: AutoReplyPrompt }>(
      req.id
        ? `${API_BASE}/auto-reply/prompts/${encodeURIComponent(req.id)}`
        : `${API_BASE}/auto-reply/prompts`,
      { method: 'POST', body: JSON.stringify(req) },
    ),
  deleteAutoReplyPrompt: (id: string) =>
    fetchJson<{ removed: number }>(`${API_BASE}/auto-reply/prompts/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  duplicateAutoReplyPrompt: (id: string) =>
    fetchJson<{ prompt: AutoReplyPrompt }>(
      `${API_BASE}/auto-reply/prompts/${encodeURIComponent(id)}/duplicate`,
      { method: 'POST' },
    ),
  exportAutoReplyPrompts: (ids: string[] = []) =>
    fetchJson<{ document: AutoReplyPromptBundle }>(
      `${API_BASE}/auto-reply/prompts/export${ids.length ? `?ids=${ids.map(encodeURIComponent).join(',')}` : ''}`,
    ),
  importAutoReplyPrompts: (document: AutoReplyPromptBundle) =>
    fetchJson<{ imported: number; skipped: number; failed: number }>(
      `${API_BASE}/auto-reply/prompts/import`,
      { method: 'POST', body: JSON.stringify(document) },
    ),
  listAutoReplyTargets: (search = '', enabledOnly = false) =>
    fetchJson<{ targets: AutoReplyTarget[] }>(
      `${API_BASE}/auto-reply/targets?search=${encodeURIComponent(search)}&enabled_only=${enabledOnly ? '1' : '0'}`,
    ),
  addAutoReplyTargets: (targets: { chat_id: string; title?: string; username?: string; note?: string }[]) =>
    fetchJson<{ added: number; targets: AutoReplyTarget[] }>(`${API_BASE}/auto-reply/targets`, {
      method: 'POST',
      body: JSON.stringify({ targets }),
    }),
  updateAutoReplyTarget: (chatId: string, patch: Partial<AutoReplyTarget>) =>
    fetchJson<{ target: AutoReplyTarget }>(
      `${API_BASE}/auto-reply/targets/${encodeURIComponent(chatId)}`,
      { method: 'PUT', body: JSON.stringify(patch) },
    ),
  removeAutoReplyTargets: (chatIds: string[]) =>
    fetchJson<{ removed: number }>(`${API_BASE}/auto-reply/targets`, {
      method: 'DELETE',
      body: JSON.stringify({ chat_ids: chatIds }),
    }),
  resolveAutoReplyChat: (query: string, sessionId = '') =>
    fetchJson<{ found: boolean; target: AutoReplyTarget | null }>(
      `${API_BASE}/auto-reply/targets/resolve`,
      { method: 'POST', body: JSON.stringify({ session_id: sessionId, query }) },
    ),
  listAutoReplyRules: () =>
    fetchJson<{ rules: AutoReplyRule[] }>(`${API_BASE}/auto-reply/rules`),
  saveAutoReplyRule: (req: SaveAutoReplyRuleRequest & { id?: string }) =>
    fetchJson<{ rule: AutoReplyRule }>(
      req.id
        ? `${API_BASE}/auto-reply/rules/${encodeURIComponent(req.id)}`
        : `${API_BASE}/auto-reply/rules`,
      { method: 'POST', body: JSON.stringify(req) },
    ),
  deleteAutoReplyRule: (id: string) =>
    fetchJson<{ removed: number }>(`${API_BASE}/auto-reply/rules/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  reorderAutoReplyRules: (ruleIds: string[]) =>
    fetchJson<{ rules: AutoReplyRule[] }>(`${API_BASE}/auto-reply/rules/reorder`, {
      method: 'POST',
      body: JSON.stringify({ rule_ids: ruleIds }),
    }),
  listAutoReplyJobs: (params: { status?: string; chat_id?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.status) q.set('status', params.status)
    if (params.chat_id) q.set('chat_id', params.chat_id)
    if (params.limit !== undefined) q.set('limit', String(params.limit))
    if (params.offset !== undefined) q.set('offset', String(params.offset))
    return fetchJson<AutoReplyJobPage>(`${API_BASE}/auto-reply/jobs?${q.toString()}`)
  },
  retryAutoReplyJob: (id: string) =>
    fetchJson<{ job: AutoReplyJob }>(`${API_BASE}/auto-reply/jobs/${encodeURIComponent(id)}/retry`, {
      method: 'POST',
    }),
  cancelAutoReplyJob: (id: string) =>
    fetchJson<{ job: AutoReplyJob }>(`${API_BASE}/auto-reply/jobs/${encodeURIComponent(id)}/cancel`, {
      method: 'POST',
    }),
  getAutoReplyStats: () =>
    fetchJson<{ stats: AutoReplyStats }>(`${API_BASE}/auto-reply/stats`),
  previewAutoReply: (inboundText: string, promptId = '', aiConfigId = '') =>
    fetchJson<{ result: AutoReplyPreviewResponse }>(`${API_BASE}/auto-reply/preview`, {
      method: 'POST',
      body: JSON.stringify({ inbound_text: inboundText, prompt_id: promptId, ai_config_id: aiConfigId }),
    }),
  // ---------------------------------------------------- Text replacement
  // A replace rule is stored once per task it runs on, but every method here
  // speaks about it as one rule: the core groups the copies by id and reports
  // the tasks it covers. Owner identity is the bearer token, never a parameter.
  listReplaceRules: (opts: {
    q?: string
    status?: string
    taskId?: string
    page?: number
    perPage?: number
  } = {}) => {
    const params = new URLSearchParams()
    if (opts.q) params.set('q', opts.q)
    if (opts.status && opts.status !== 'all') params.set('status', opts.status)
    if (opts.taskId) params.set('task_id', opts.taskId)
    if (opts.page && opts.page > 1) params.set('page', String(opts.page))
    if (opts.perPage) params.set('per_page', String(opts.perPage))
    const qs = params.toString()
    return fetchJson<ReplaceRulePage>(`${API_BASE}/replace-rules${qs ? `?${qs}` : ''}`)
  },
  getReplaceRule: (id: string) =>
    fetchJson<{ rule: ReplaceRule }>(`${API_BASE}/replace-rules/${encodeURIComponent(id)}`),
  createReplaceRule: (req: ReplaceRuleInput) =>
    fetchJson<{ rule: ReplaceRule }>(`${API_BASE}/replace-rules`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  updateReplaceRule: (id: string, req: ReplaceRuleInput) =>
    fetchJson<{ rule: ReplaceRule }>(`${API_BASE}/replace-rules/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(req),
    }),
  deleteReplaceRule: (id: string) =>
    fetchJson<{ removed: number }>(`${API_BASE}/replace-rules/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  setReplaceRuleEnabled: (id: string, enabled: boolean) =>
    fetchJson<{ rule: ReplaceRule }>(
      `${API_BASE}/replace-rules/${encodeURIComponent(id)}/enable`,
      { method: 'POST', body: JSON.stringify({ enabled }) },
    ),
  duplicateReplaceRule: (id: string, name = '') =>
    fetchJson<{ rule: ReplaceRule }>(
      `${API_BASE}/replace-rules/${encodeURIComponent(id)}/duplicate`,
      { method: 'POST', body: JSON.stringify({ name }) },
    ),
  testReplaceRule: (ruleId: string, sample: string, draft: ReplaceRuleInput = {}) =>
    fetchJson<ReplaceRuleTestResult>(`${API_BASE}/replace-rules/test`, {
      method: 'POST',
      body: JSON.stringify({ rule_id: ruleId, sample, ...draft }),
    }),
  listReplaceRuleTasks: () =>
    fetchJson<{ tasks: ReplaceTaskOption[] }>(`${API_BASE}/replace-rules/tasks`),

  // ------------------------------------------------------------ Diagnostics
  diagnostics: () => fetchJson<DiagnosticsResponse>(`${API_BASE}/diagnostics`),

  // ------------------------------------------------- Client Self-Management
  clientGetSettings: () => fetchJson<any>(`${API_BASE}/client/settings`),
  clientSaveCredentials: (apiId: number, apiHash: string) =>
    fetchJson<{ success: boolean; message: string }>(`${API_BASE}/client/credentials`, {
      method: 'POST',
      body: JSON.stringify({ api_id: apiId, api_hash: apiHash }),
    }),
}
