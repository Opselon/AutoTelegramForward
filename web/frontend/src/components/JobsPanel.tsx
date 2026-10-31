import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Plus, X, Ban, RotateCw, Loader2 } from 'lucide-react'
import { api } from '../api'
import type { UserTask, TaskKind } from '../types'
import { makeT, type UiLang } from '../locales/engine'

/**
 * The web dashboard's view of the per-user task lifecycle (the same
 * /api/tasks contract every other platform uses). Identity is the caller's
 * token — the panel never sends an owner id and the server would ignore one.
 */

const POLL_INTERVAL_MS = 4000

const STATUS_ORDER: Record<string, number> = {
  RUNNING: 0,
  RETRY_WAIT: 1,
  PENDING: 2,
  FAILED: 3,
  CANCELLED: 4,
  SUCCEEDED: 5,
}

// Bounded job kinds the panel exposes as one-click submissions — these are the
// same kind values the Go API allowlists before the RPC. The label and its hint
// are catalog keys, so a submission form is readable in every language.
const KIND_OPTIONS: { value: TaskKind; labelKey: string; hintKey: string; fields: number }[] = [
  { value: 'session.backup', labelKey: 'ui_job_backup_session', hintKey: 'ui_job_backup_session_hint', fields: 1 },
  { value: 'dlq.replay', labelKey: 'ui_job_replay_dead_letters', hintKey: 'ui_job_replay_dead_letters_hint', fields: 1 },
  { value: 'dlq.purge', labelKey: 'ui_job_purge_dead_letters', hintKey: 'ui_job_purge_dead_letters_hint', fields: 1 },
  { value: 'backup.export', labelKey: 'ui_job_export_backup', hintKey: 'ui_job_export_backup_hint', fields: 0 },
]

function fmt(ts?: number): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString()
}

export function JobsPanel({
  lang,
  onPendingChange,
}: {
  lang: UiLang
  /** Optional report of the pending/running count for the tab badge. */
  onPendingChange?: (n: number) => void
}) {
  const t = makeT(lang)
  const [tasks, setTasks] = useState<UserTask[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [openCreate, setOpenCreate] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const page = await api.listTasks(100, 0)
      const sorted = (page.tasks || []).slice().sort((a: UserTask, b: UserTask) => {
        const s = (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9)
        return s || b.updated_at - a.updated_at
      })
      setTasks(sorted)
      setTotal(page.total ?? sorted.length)
      setError(null)
    } catch (e: any) {
      setError(e?.message || t('ui_failed_to_load'))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(refresh, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [autoRefresh, refresh])

  // The tab badge ("My Jobs (n)") tracks the same numbers the panel shows.
  const pending = tasks.filter(
    (t) => t.status === 'PENDING' || t.status === 'RUNNING' || t.status === 'RETRY_WAIT',
  ).length
  useEffect(() => {
    onPendingChange?.(pending)
  }, [pending, onPendingChange])

  const act = async (task: UserTask, op: 'cancel' | 'retry') => {
    setBusy(task.id + op)
    try {
      await (op === 'cancel' ? api.cancelTask(task.id) : api.retryTask(task.id))
      await refresh()
    } catch (e: any) {
      setError(e?.message || t('ui_failed'))
    } finally {
      setBusy(null)
    }
  }

  const statusColor = (s: string) =>
    s === 'SUCCEEDED' ? 'var(--color-success)'
    : s === 'FAILED' ? 'var(--color-danger)'
    : s === 'CANCELLED' ? 'var(--color-accent-violet)'
    : s === 'RUNNING' ? 'var(--color-info)'
    : s === 'RETRY_WAIT' ? 'var(--color-warning)'
    : 'var(--color-text-muted)'

  return (
    <div className="panel-container">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, gap: 8, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0, fontSize: '1.15rem' }}>{t('ui_my_jobs')}</h2>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn" onClick={() => setAutoRefresh((v) => !v)}
            style={{ background: autoRefresh ? 'var(--color-primary-soft)' : 'transparent', color: autoRefresh ? 'var(--color-primary)' : 'var(--color-text-muted)' }}>
            <RefreshCw size={14} />
            <span>{t('ui_auto_renew')}</span>
          </button>
          <button className="btn" onClick={() => setOpenCreate(true)}
            style={{ background: 'var(--color-primary-soft)', color: 'var(--color-primary)', border: '1px solid color-mix(in oklab, var(--color-primary) 40%, transparent)' }}>
            <Plus size={14} />
            <span>{t('ui_new_job')}</span>
          </button>
        </div>
      </div>

      {error && (
        <div style={{ border: '1px solid color-mix(in oklab, var(--color-danger) 40%, transparent)', background: 'var(--color-danger-soft)', color: 'var(--color-danger)', borderRadius: 8, padding: '8px 12px', marginBottom: 12 }}>
          {error}
        </div>
      )}

      {loading ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--color-text-muted)' }}>
          <Loader2 size={15} />{t('ui_loading_jobs')}
        </div>
      ) : tasks.length === 0 ? (
        <div style={{ color: 'var(--color-text-muted)', textAlign: 'center', padding: '32px 0' }}>
          {t('ui_no_jobs_yet_long_running_operations_session_backups_dlq_replays')}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
            {t('ui_showing')} {tasks.length} / {total}
          </div>
          {tasks.map((task) => {
            const canCancel = task.status === 'PENDING'
            const canRetry = task.status === 'FAILED' || task.status === 'CANCELLED'
            return (
              <div key={task.id}
                style={{
                  border: '1px solid var(--color-border)', borderRadius: 8,
                  background: 'var(--color-surface-2)', direction: 'ltr',
                  padding: '10px 12px', display: 'grid',
                  gridTemplateColumns: 'auto 1fr auto', gap: '8px 14px', alignItems: 'center',
                }}>
                <span style={{ fontSize: 'var(--type-xs)', fontWeight: 700, color: statusColor(task.status), minWidth: 96, textAlign: 'center' }}>
                  {task.status}{task.status === 'RETRY_WAIT' ? ` (${task.attempt}/${task.max_attempts})` : ''}
                </span>
                <div>
                  <div style={{ fontVariantLigatures: 'none', fontFamily: 'monospace', fontSize: '0.8rem', color: 'var(--color-text)' }}>
                    {task.kind}
                  </div>
                  {task.error && (
                    <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-danger)', marginTop: 2 }}>{task.error}</div>
                  )}
                  <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginTop: 2 }}>
                    #{task.id.slice(0, 12)} · {fmt(task.created_at)} → {fmt(task.finished_at)}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6, flexDirection: lang === 'fa' ? 'row-reverse' : 'row' }}>
                  {canCancel && (
                    <button className="btn" onClick={() => act(task, 'cancel')} disabled={busy === task.id + 'cancel'}
                      style={{ background: 'var(--color-danger-soft)', color: 'var(--color-danger)', border: '1px solid color-mix(in oklab, var(--color-danger) 40%, transparent)', padding: '4px 8px' }}>
                      {busy === task.id + 'cancel' ? <Loader2 size={13} /> : <Ban size={13} />}
                      <span>{t('ui_cancel')}</span>
                    </button>
                  )}
                  {canRetry && (
                    <button className="btn" onClick={() => act(task, 'retry')} disabled={busy === task.id + 'retry'}
                      style={{ background: 'var(--color-success-soft)', color: 'var(--color-success)', border: '1px solid color-mix(in oklab, var(--color-success) 40%, transparent)', padding: '4px 8px' }}>
                      {busy === task.id + 'retry' ? <Loader2 size={13} /> : <RotateCw size={13} />}
                      <span>{t('ui_retry')}</span>
                    </button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}

      {openCreate && (
        <CreateTaskModal lang={lang} onClose={() => setOpenCreate(false)} onCreated={() => { setOpenCreate(false); refresh() }} />
      )}
    </div>
  )
}

function CreateTaskModal({ lang, onClose, onCreated }: { lang: UiLang; onClose: () => void; onCreated: () => void }) {
  const t = makeT(lang)
  const [kind, setKind] = useState<TaskKind>('session.backup')
  const [field0, setField0] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const opt = KIND_OPTIONS.find((o) => o.value === kind)!

  const submit = async () => {
    setSubmitting(true)
    setErr(null)
    try {
      const payload: Record<string, any> = {}
      if (opt.fields > 0) {
        const v = field0.trim()
        if (!v) throw new Error(t(opt.hintKey))
        if (kind === 'session.backup') payload.session_id = v
        else if (kind === 'dlq.replay') payload.delivery_id = v
        else if (kind === 'dlq.purge') payload.rule_id = v === 'all' ? undefined : v
      }
      await api.createTask(kind, payload)
      onCreated()
    } catch (e: any) {
      setErr(e?.message || 'Failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="modal-overlay"
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 'var(--z-modal)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'var(--color-scrim)', padding: 16,
      }}
    >
      <div
        className="glass-panel modal-sheet"
        onClick={(e) => e.stopPropagation()}
        style={{ padding: 20, width: '100%', maxWidth: 420, direction: 'ltr' }}
      >
        <header className="modal-head">
          <h3 className="modal-title">{t('ui_new_background_job')}</h3>
          <button className="btn btn--ghost btn--icon" onClick={onClose}><X size={15} /></button>
        </header>

        <div className="modal-sheet-body" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {KIND_OPTIONS.map((o) => (
            <label key={o.value} className="p-row" style={{ alignItems: 'flex-start', cursor: 'pointer' }}>
              <input type="radio" checked={kind === o.value} onChange={() => { setKind(o.value); setField0('') }} />
              <div>
                <div style={{ fontSize: '0.9rem' }}>{t(o.labelKey)}</div>
                <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>{t(o.hintKey)}</div>
              </div>
            </label>
          ))}
          {opt.fields > 0 && (
            <div className="field">
              <input className="input-field" value={field0} onChange={(e) => setField0(e.target.value)}
                placeholder={t(opt.hintKey)} />
            </div>
          )}
          {err && <div className="p-note p-note--danger">{err}</div>}
          <div className="p-row" style={{ justifyContent: 'flex-end' }}>
            <button className="btn btn--primary" onClick={submit} disabled={submitting}>
              {submitting ? <Loader2 size={14} /> : <Plus size={14} />}
              <span>{t('ui_submit')}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
