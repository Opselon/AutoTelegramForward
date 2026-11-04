/**
 * Go Worker operational surface — TypeScript views of the /ops contract
 * (worker-side shapes live in worker/internal/health; these mirror the
 * documented JSON, nothing more).
 *
 * The worker is the single telemetry owner and the API only proxies it, so
 * every successful fetch is real worker state. An unreachable worker is
 * `worker_reachable: false` and the UI renders that as OFFLINE — never as
 * zeroes that would look like an idle, healthy worker.
 *
 * Contract: /root/.hermes/cache/scratch/devops-contract.md §1-2.
 */

/** 503 body shared by every worker route. */
export interface WorkerUnreachable {
  worker_reachable: false
  error: string
}

export interface WorkerSwitches {
  worker_enabled: boolean
  transforms: boolean
  autoreply: boolean
}

export interface WorkerInfo {
  state: 'starting' | 'ready' | 'degraded' | 'stopping'
  worker_id: string
  commit_sha: string
  version: string
  uptime_seconds: number
  started_at: string
  goroutines: number
  cpu_percent: number
  memory_bytes: number
  num_workers: number
  active: number
  /** active / num_workers, 0..1 — NOT goroutines. */
  utilization: number
  processing_mode: 'worker' | 'legacy'
  switches: WorkerSwitches
  queue_backend: string
  db_connected: boolean
  telegram_connected: boolean
  ai_connected: boolean
  last_heartbeat: string
}

export interface LatencyPercentiles {
  p50: number
  p95: number
  p99: number
  units: string
}

export type LoadStatus =
  | 'idle'
  | 'normal'
  | 'high'
  | 'saturated'
  | 'degraded'
  | 'unhealthy'

export interface WorkerQueue {
  pending: number
  processing: number
  delivered: number
  failed: number
  delayed: number
  retry_pending: number
  dead_letter: number
  capacity: number
  jobs_per_second: number
  latency: LatencyPercentiles
  queue_wait: LatencyPercentiles
  oldest_pending_age_seconds: number
  backlog_trend: number[]
  load_status: LoadStatus
}

export type StageHealth =
  | 'healthy'
  | 'degraded'
  | 'failed'
  | 'disabled'
  | 'waiting'

export const PIPELINE_STAGES = [
  'ingestion',
  'queue',
  'task_resolution',
  'filter',
  'replacement',
  'cleaner',
  'header_footer',
  'ai',
  'validation',
  'delivery',
  'reconciliation',
] as const
export type PipelineStageName = (typeof PIPELINE_STAGES)[number]

export interface PipelineStageRow {
  stage: PipelineStageName | string
  enabled: boolean
  health: StageHealth
  processed: number
  latency_ms_avg: number
  failures: number
  last_error: string | null
  last_run_at: string | null
  // Optional per-stage extras (e.g. the filter's policy skips).
  skipped_by_policy?: number
  skipped_reason?: string | null
}

export interface WorkerTaskFeatures {
  filter: boolean
  replacement: boolean
  ai: boolean
  cleaner: boolean
  header: boolean
  footer: boolean
  fen: number
  media: boolean
  fallback: boolean
}

export interface WorkerTaskRow {
  rule_id: string
  source: string
  target: string
  enabled: boolean
  pending: number
  processing: number
  delivered: number
  failed: number
  retries: number
  last_success_at: string | null
  last_error: string | null
  features: WorkerTaskFeatures
  throughput_per_min: number
  latency_ms_avg: number
  worker_id: string
  config_updated_at: string | null
}

export type ErrorSeverity = 'ERROR' | 'WARN' | 'INFO'

export interface WorkerErrorItem {
  ts: string
  severity: ErrorSeverity | string
  component: string
  worker_id: string
  rule_id: string | null
  job_id: string | null
  category: string
  code: string
  retryable: boolean
  retry_count: number
  correlation_id: string | null
  message: string
  // Dedup ring extras: repeated occurrences collapse into one row.
  count?: number
  first_seen?: string
}

export interface WorkerErrorsBody {
  total: number
  items: WorkerErrorItem[]
}

/** Successful /worker/status is the worker's /ops object + reachable flag. */
export type WorkerStatusResponse = (WorkerInfo & { worker_reachable?: true }) | WorkerUnreachable
export type WorkerQueueResponse = WorkerQueue | WorkerUnreachable
export interface WorkerTasksBody {
  tasks: WorkerTaskRow[]
}
export type WorkerTasksResponse = WorkerTasksBody | WorkerUnreachable
export type WorkerErrorsResponse = WorkerErrorsBody | WorkerUnreachable

export interface WorkerErrorQuery {
  severity?: string
  component?: string
  rule_id?: string
  since?: number
  limit?: number
}

/** Type guard: a route response is the 503 unreachable shape. */
export function isWorkerUnreachable(r: unknown): r is WorkerUnreachable {
  return (
    typeof r === 'object' &&
    r !== null &&
    (r as WorkerUnreachable).worker_reachable === false
  )
}
