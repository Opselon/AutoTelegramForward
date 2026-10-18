import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Plus, X, Ban, RotateCw, Loader2 } from 'lucide-react'
import { api } from '../api'
import type { UserTask, TaskKind } from '../types'

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
// same kind values the Go API allowlists before the RPC.
const KIND_OPTIONS: { value: TaskKind; fa: string; en: string; hintEn: string; hintFa: string; fields: number }[] = [
  { value: 'session.backup', fa: 'پشتیبان‌گیری سشن', en: 'Backup a session', hintEn: 'Session id from the Sessions tab', hintFa: 'شناسه سشن از تب سشن‌ها', fields: 1 },
  { value: 'dlq.replay', fa: 'اجرای مجدد رد شده‌ها', en: 'Replay dead letters', hintEn: 'Delivery id from the DLQ list', hintFa: 'شناسه تحویل از فهرست DLQ', fields: 1 },
  { value: 'dlq.purge', fa: 'پاکسازی رد شده‌ها', en: 'Purge dead letters', hintEn: 'Optional: rule id; or "all"', hintFa: 'اختیاری: شناسه قانون یا همه', fields: 1 },
  { value: 'backup.export', fa: 'خروجی پشتیبان', en: 'Export a backup', hintEn: 'No additional fields', hintFa: 'بدون فیلد اضافه', fields: 0 },
]

function fmt(ts?: number): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString()
}

export function JobsPanel({
  lang,
  onPendingChange,
}: {
  lang: 'fa' | 'en'
  /** Optional report of the pending/running count for the tab badge. */
  onPendingChange?: (n: number) => void
}) {
  const isFa = lang === 'fa'
  const t = (en: string, fa: string) => (isFa ? fa : en)
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
      setError(e?.message || 'Failed to load')
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
      setError(e?.message || 'Failed')
    } finally {
      setBusy(null)
    }
  }

  const statusColor = (s: string) =>
    s === 'SUCCEEDED' ? '#4ade80'
    : s === 'FAILED' ? '#f87171'
    : s === 'CANCELLED' ? '#a78bfa'
    : s === 'RUNNING' ? '#38bdf8'
    : s === 'RETRY_WAIT' ? '#fbbf24'
    : '#94a3b8'

  return (
    <div className="panel-container">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, gap: 8, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0, fontSize: '1.15rem' }}>{t('My Jobs', 'وظایف من')}</h2>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn" onClick={() => setAutoRefresh((v) => !v)}
            style={{ background: autoRefresh ? 'rgba(20, 184, 166, 0.2)' : 'transparent', color: autoRefresh ? '#2dd4bf' : '#94a3b8' }}>
            <RefreshCw size={14} />
            <span>{t('Auto-renew', 'به‌روزرسانی خودکار')}</span>
          </button>
          <button className="btn" onClick={() => setOpenCreate(true)}
            style={{ background: 'rgba(20, 184, 166, 0.2)', color: '#2dd4bf', border: '1px solid rgba(20, 184, 166, 0.4)' }}>
            <Plus size={14} />
            <span>{t('New job', 'وظیفه جدید')}</span>
          </button>
        </div>
      </div>

      {error && (
        <div style={{ border: '1px solid rgba(239,68,68,0.4)', background: 'rgba(239,68,68,0.1)', color: '#f87171', borderRadius: 8, padding: '8px 12px', marginBottom: 12 }}>
          {error}
        </div>
      )}

      {loading ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: '#94a3b8' }}>
          <Loader2 size={15} />{t('Loading jobs…', 'در حال بارگذاری…')}
        </div>
      ) : tasks.length === 0 ? (
        <div style={{ color: '#94a3b8', textAlign: 'center', padding: '32px 0' }}>
          {t('No jobs yet. Long-running operations (session backups, DLQ replays/purges, exports) appear here with their progress and result.',
             'هیچ وظیفه‌ای نیست. عملیات طولانی (پشتیبان‌گیری سشن، اجرای مجدد/پاکسازی DLQ، خروجی پشتیبان) با پیشرفت و نتیجه در این‌جا نمایش داده می‌شوند.')}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
            {t('showing', 'نمایش')} {tasks.length} / {total}
          </div>
          {tasks.map((task) => {
            const canCancel = task.status === 'PENDING'
            const canRetry = task.status === 'FAILED' || task.status === 'CANCELLED'
            return (
              <div key={task.id}
                style={{
                  border: '1px solid rgba(255,255,255,0.08)', borderRadius: 8,
                  background: 'rgba(255,255,255,0.03)', direction: 'ltr',
                  padding: '10px 12px', display: 'grid',
                  gridTemplateColumns: 'auto 1fr auto', gap: '8px 14px', alignItems: 'center',
                }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: statusColor(task.status), minWidth: 96, textAlign: 'center' }}>
                  {task.status}{task.status === 'RETRY_WAIT' ? ` (${task.attempt}/${task.max_attempts})` : ''}
                </span>
                <div>
                  <div style={{ fontVariantLigatures: 'none', fontFamily: 'monospace', fontSize: '0.8rem', color: '#e2e8f0' }}>
                    {task.kind}
                  </div>
                  {task.error && (
                    <div style={{ fontSize: '0.75rem', color: '#f87171', marginTop: 2 }}>{task.error}</div>
                  )}
                  <div style={{ fontSize: '0.7rem', color: '#94a3b8', marginTop: 2 }}>
                    #{task.id.slice(0, 12)} · {fmt(task.created_at)} → {fmt(task.finished_at)}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6, flexDirection: lang === 'fa' ? 'row-reverse' : 'row' }}>
                  {canCancel && (
                    <button className="btn" onClick={() => act(task, 'cancel')} disabled={busy === task.id + 'cancel'}
                      style={{ background: 'rgba(239,68,68,0.15)', color: '#f87171', border: '1px solid rgba(239,68,68,0.4)', padding: '4px 8px' }}>
                      {busy === task.id + 'cancel' ? <Loader2 size={13} /> : <Ban size={13} />}
                      <span>{t('Cancel', 'لغو')}</span>
                    </button>
                  )}
                  {canRetry && (
                    <button className="btn" onClick={() => act(task, 'retry')} disabled={busy === task.id + 'retry'}
                      style={{ background: 'rgba(34,197,94,0.15)', color: '#4ade80', border: '1px solid rgba(34,197,94,0.4)', padding: '4px 8px' }}>
                      {busy === task.id + 'retry' ? <Loader2 size={13} /> : <RotateCw size={13} />}
                      <span>{t('Retry', 'اجرای مجدد')}</span>
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

function CreateTaskModal({ lang, onClose, onCreated }: { lang: 'fa' | 'en'; onClose: () => void; onCreated: () => void }) {
  const isFa = lang === 'fa'
  const t = (en: string, fa: string) => (isFa ? fa : en)
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
        if (!v) throw new Error(opt.hintEn)
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
    <div onClick={onClose}
      style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50, padding: 16 }}>
      <div onClick={(e) => e.stopPropagation()}
        style={{ background: '#1e293b', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 12, padding: 20, width: '100%', maxWidth: 420, direction: 'ltr' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 14 }}>
          <h3 style={{ margin: 0 }}>{t('New background job', 'وظیفه پس‌زمینه جدید')}</h3>
          <button className="btn" onClick={onClose} style={{ padding: '4px 8px' }}><X size={15} /></button>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {KIND_OPTIONS.map((o) => (
            <label key={o.value} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', cursor: 'pointer' }}>
              <input type="radio" checked={kind === o.value} onChange={() => { setKind(o.value); setField0('') }} />
              <div>
                <div style={{ fontSize: '0.9rem' }}>{isFa ? o.fa : o.en}</div>
                <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>{isFa ? o.hintFa : o.hintEn}</div>
              </div>
            </label>
          ))}
          {opt.fields > 0 && (
            <input className="input" value={field0} onChange={(e) => setField0(e.target.value)}
              placeholder={isFa ? opt.hintFa : opt.hintEn}
              style={{ border: '1px solid rgba(255,255,255,0.15)', borderRadius: 8, padding: 8, background: 'rgba(0,0,0,0.3)' }} />
          )}
          {err && <div style={{ color: '#f87171', fontSize: '0.8rem' }}>{err}</div>}
          <button className="btn" onClick={submit} disabled={submitting}
            style={{ background: 'rgba(20, 184, 166, 0.25)', color: '#2dd4bf', justifySelf: 'end' }}>
            {submitting ? <Loader2 size={14} /> : <Plus size={14} />}
            <span>{t('Submit', 'ارسال')}</span>
          </button>
        </div>
      </div>
    </div>
  )
}
