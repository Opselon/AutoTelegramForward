import type {
  ForwardRule,
  SaveRuleRequest,
  RoutePathQuickSetRequest,
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
  SimulateRequest,
  SimulateResponse,
  StatsResponse,
  LogItem,
  LogStats,
  AuthResponse,
  PVResponderConfig,
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

export const api = {
  getStats: () => fetchJson<StatsResponse>(`${API_BASE}/stats`),
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
    const q = new URLSearchParams()
    if (params?.service) q.set('service', params.service)
    if (params?.level) q.set('level', params.level)
    if (params?.search) q.set('search', params.search)
    if (params?.limit) q.set('limit', String(params.limit))
    const url = `${API_BASE}/logs${q.toString() ? '?' + q.toString() : ''}`
    const res = await fetchJson<any>(url)
    if (Array.isArray(res)) return res
    if (Array.isArray(res?.logs)) return res.logs
    if (Array.isArray(res?.items)) return res.items
    return []
  },
  getLogStats: async (): Promise<LogStats> => {
    return fetchJson<LogStats>(`${API_BASE}/logs/stats`)
  },

  // ------------------------------------------------- Authentication & Identity
  register: (req: { username: string; password: string; display_name?: string }) =>
    fetchJson<AuthResponse>(`${API_BASE}/auth/register`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  login: (req: { username: string; password: string }) =>
    fetchJson<AuthResponse>(`${API_BASE}/auth/login`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  exchangeBotToken: (req: { user_id: number }) =>
    fetchJson<AuthResponse>(`${API_BASE}/auth/exchange-bot-token`, {
      method: 'POST',
      body: JSON.stringify(req),
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
}
