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
  GatewaySystemInfo,
  SimulateRequest,
  SimulateResponse,
  StatsResponse,
  LogItem,
} from './types'

const API_BASE = '/api'

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })
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
  getRules: () => fetchJson<ForwardRule[]>(`${API_BASE}/rules`),
  getRule: (id: string) => fetchJson<ForwardRule>(`${API_BASE}/rules/${id}`),
  saveRule: (req: SaveRuleRequest) =>
    fetchJson<ForwardRule>(`${API_BASE}/rules`, {
      method: 'POST',
      body: JSON.stringify(req),
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

  // Sessions
  getSessions: () => fetchJson<Session[]>(`${API_BASE}/sessions`),

  // AI Configurations
  getAIConfigs: () => fetchJson<AIConfig[]>(`${API_BASE}/ai-configs`),
  saveAIConfig: (req: SaveAIConfigRequest) =>
    fetchJson<AIConfig>(`${API_BASE}/ai-configs`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  deleteAIConfig: (id: string) =>
    fetchJson<{ success: boolean; id: string }>(`${API_BASE}/ai-configs/${id}`, {
      method: 'DELETE',
    }),

  // Filter Rules
  getFilters: () => fetchJson<FilterRule[]>(`${API_BASE}/filters`),
  saveFilter: (req: SaveFilterRuleRequest) =>
    fetchJson<FilterRule>(`${API_BASE}/filters`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
  deleteFilter: (id: string) =>
    fetchJson<{ success: boolean; id: string }>(`${API_BASE}/filters/${id}`, {
      method: 'DELETE',
    }),

  // Queue & DLQ
  getQueueJobs: () => fetchJson<DeliveryJob[]>(`${API_BASE}/queue`),
  getDLQJobs: () => fetchJson<DeadLetterJob[]>(`${API_BASE}/dlq`),
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
  getLogs: () => fetchJson<LogItem[]>(`${API_BASE}/logs`),
}
