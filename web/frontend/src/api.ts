import type {
  AIConfig,
  AuthResponse,
  BotTokenInfo,
  BackupExportRequest,
  BackupExportResponse,
  BackupImportRequest,
  BackupImportResponse,
  DeadLetterJob,
  DeliveryJob,
  DeliveryStats,
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
  SaveAIConfigRequest,
  SaveFilterRuleRequest,
  SavePromptTemplateRequest,
  SaveRuleRequest,
  ServicesHealthResponse,
  Session,
  SetupReport,
  SimulateRequest,
  SimulateResponse,
  StatsResponse,
  TestPromptRequest,
  TestPromptResponse,
} from './types'

const API_BASE = '/api'

// Shared single fetch of /queue so getDeliveryStats/getRuleLiveStats/getRecentErrors
// don't issue three redundant network calls on every render.
let _queueCache: { data: any; ts: number } | null = null
const QUEUE_TTL = 4000
async function getDeliveryStatsAny(): Promise<any> {
  const now = Date.now()
  if (_queueCache && now - _queueCache.ts < QUEUE_TTL) {
    return _queueCache.data
  }
  try {
    const res = await fetchJson<any>(`${API_BASE}/queue`)
    _queueCache = { data: res, ts: now }
    return res
  } catch {
    return null
  }
}

/** Bearer header for raw fetches (downloads, blobs) outside fetchJson. */
function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('auth_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const token = localStorage.getItem('auth_token')
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
    try {
      localStorage.removeItem('auth_token')
    } catch {}
    window.dispatchEvent(new Event('auth_required'))
    throw new Error('Authentication required')
  }
  if (!res.ok) {
    const errText = await res.text()
    throw new Error(errText || `HTTP ${res.status}`)
  }
  return res.json()
}

/** Pull the server's own message out of an error body ("{"error":"..."}"). */
function reasonOf(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && typeof parsed.error === 'string') {
      return parsed.error
    }
  } catch {}
  return raw
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
  ) =>
    fetchJson<RuleSettingsResponse>(
      `${API_BASE}/rules/${encodeURIComponent(id)}/settings`,
      {
        method: 'PATCH',
        body: JSON.stringify({ settings, replace_all: replaceAll }),
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
  createBotToken: async (token: string, label = ''): Promise<BotTokenInfo> => {
    try {
      const res = await fetchJson<any>(`${API_BASE}/bot-tokens`, {
        method: 'POST',
        body: JSON.stringify({ token, label }),
      })
      return res?.token ?? res
    } catch (err) {
      // The API answers {"error": "...", "code": "..."}: surface the reason, not
      // the raw body — the panel shows it verbatim to the user.
      throw new Error(reasonOf(err))
    }
  },
  deleteBotToken: async (id: string): Promise<void> => {
    try {
      await fetchJson<any>(`${API_BASE}/bot-tokens/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      })
    } catch (err) {
      throw new Error(reasonOf(err))
    }
  },
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
  stopRule: (id: string) =>
    fetchJson<ForwardRule>(`${API_BASE}/rules/${id}/stop`, {
      method: 'POST',
    }),
  restartRule: (id: string) =>
    fetchJson<ForwardRule>(`${API_BASE}/rules/${id}/restart`, {
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
  deleteFilter: (id: string) =>
    fetchJson<{ success: boolean; id: string }>(`${API_BASE}/filters/${id}`, {
      method: 'DELETE',
    }),

  // Queue & DLQ
  getQueueJobs: async (): Promise<DeliveryJob[]> => {
    const res = await fetchJson<any>(`${API_BASE}/queue`)
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
      const res = await fetchJson<any>(`${API_BASE}/queue`)
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
    const res = await getDeliveryStatsAny()
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
  },
  getRecentErrors: async (): Promise<RecentError[]> => {
    const res = await getDeliveryStatsAny()
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
  },
  getDLQJobs: async (): Promise<DeadLetterJob[]> => {
    const res = await fetchJson<any>(`${API_BASE}/dlq`)
    if (Array.isArray(res)) return res
    if (Array.isArray(res?.jobs)) return res.jobs
    if (Array.isArray(res?.entries)) return res.entries
    return []
  },
  retryDLQ: (id: string) =>
    fetchJson<{ success: boolean; retried_id: string }>(`${API_BASE}/dlq/${id}/retry`, {
      method: 'POST',
    }),
  purgeDLQ: () =>
    fetchJson<{ success: boolean; purged_count: number }>(`${API_BASE}/dlq`, {
      method: 'DELETE',
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
    return fetchJson<ServicesHealthResponse>('/api/v1/health/services')
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
  exchangeBotToken: (req: { user_id: number }) =>
    fetchJson<AuthResponse>(`${API_BASE}/auth/exchange-bot-token`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  authTMA: (initData: string) =>
    fetchJson<AuthResponse>(`${API_BASE}/auth/tma`, {
      method: 'POST',
      body: JSON.stringify({ init_data: initData }),
    }),
  changePassword: (req: { old_password?: string; new_password: string }) =>
    fetchJson<{ success: boolean; message: string }>(`${API_BASE}/auth/change-password`, {
      method: 'PATCH',
      body: JSON.stringify(req),
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
    try {
      localStorage.removeItem('auth_token')
    } catch {}
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
