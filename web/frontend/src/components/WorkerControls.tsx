/**
 * WorkerControls — the §10 operational-action panel.
 *
 * The contract's exact requirements: support pause/resume, require explicit
 * confirmation for disruptive actions, an audit trail, and never let a
 * worker falsely report an operation simply because the UI accepted the
 * request. Every action here lands on the worker and comes back with the
 * operation the WORKER recorded — the pending spinner is that op's real
 * asynchronous status, and a no-op (pause while already paused) renders
 * with the honest "already paused" reason instead of a state change.
 *
 * In-flight deliveries are NOT cancelled by a pause: the contract forbids
 * a mid-job abort that could leave a half-sent album, so the pause is
 * stated as exactly what it does. Restart/shutdown are intentionally absent
 * here: they need a supervisor this deployment does not have, and an
 * honest "not available" beats a fabricated restart that never happened.
 */
import { useCallback, useEffect, useState } from 'react'
import { Pause, Play, Activity, AlertTriangle, Loader2 } from 'lucide-react'
import { api } from '../api'
import { ConfirmDialog } from './ConfirmDialog'
import { isWorkerUnreachable, type ControlOp } from '../workerOps'
import { makeT, type UiLang } from '../locales/engine'

const POLL_MS = 5_000

function fmtTs(ts?: string): string {
  if (!ts) return '—'
  const ms = Date.parse(ts)
  if (!Number.isFinite(ms)) return '—'
  return new Date(ms).toISOString().replace('T', ' ').replace('.000Z', 'Z')
}

export function WorkerControls({ lang }: { lang: UiLang }) {
  const t = makeT(lang)
  const [paused, setPaused] = useState<boolean | null>(null)
  const [pauseBy, setPauseBy] = useState<string | null>(null)
  const [pauseAt, setPauseAt] = useState<string | null>(null)
  const [ops, setOps] = useState<ControlOp[] | null>(null)
  const [available, setAvailable] = useState(true)
  const [unreachable, setUnreachable] = useState(false)
  const [busy, setBusy] = useState<'pause' | 'resume' | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Pause is disruptive: it gates through the shared alertdialog instead of a
  // blocking window.confirm, like every other confirm in the dashboard.
  const [confirmPause, setConfirmPause] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const r = await api.workerControlStatus()
      if (isWorkerUnreachable(r)) {
        setUnreachable(true)
        setPaused(null)
      } else {
        setUnreachable(false)
        setAvailable(r.available !== false)
        setPaused(!!r.paused)
        setPauseBy(r.pause?.requested_by ?? null)
        setPauseAt(r.pause?.requested_at ?? null)
        setOps(r.ops ?? [])
      }
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = setInterval(() => void refresh(), POLL_MS)
    return () => clearInterval(timer)
  }, [refresh])

  const act = useCallback(
    async (which: 'pause' | 'resume') => {
      setBusy(which)
      try {
        const r = which === 'pause' ? await api.workerControlPause() : await api.workerControlResume()
        if (isWorkerUnreachable(r)) {
          setError(t('ui_control_worker_unreachable'))
        }
        await refresh()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setBusy(null)
      }
    },
    [refresh, t],
  )

  return (
    <div className="ops-console" role="region" aria-label={t('ui_worker_controls')}>
      <div className="ops-head">
        <Activity size={18} />
        <span className="ops-head__title">{t('ui_worker_controls')}</span>
        <span className="ops-head__meta">
          {paused != null ? (
            <span
              className="ops-levelchip"
              style={{
                background: paused ? 'var(--color-warning)' : 'var(--color-success)',
                color: 'var(--color-button-fg)',
              }}
            >
              {paused ? t('ui_control_paused') : t('ui_control_running')}
            </span>
          ) : null}
        </span>
      </div>

      {unreachable ? (
        <div className="ops-empty" style={{ padding: '16px 2px', color: 'var(--color-danger)', display: 'flex', gap: 8, alignItems: 'center' }}>
          <AlertTriangle size={15} aria-hidden="true" />
          <span>{t('ui_control_worker_unreachable')}</span>
        </div>
      ) : !available ? (
        <div className="ops-empty" style={{ padding: '16px 2px', color: 'var(--color-text-muted)' }}>
          {t('ui_control_unavailable')}
        </div>
      ) : (
        <>
          <div className="ops-filter" style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', padding: '8px 2px' }}>
            {paused ? (
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => void act('resume')}
                disabled={busy !== null}
              >
                {busy === 'resume' ? <Loader2 size={13} className="spin" aria-hidden="true" /> : <Play size={13} aria-hidden="true" />}
                {t('ui_control_resume')}
              </button>
            ) : (
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => setConfirmPause(true)}
                disabled={busy !== null}
              >
                {busy === 'pause' ? <Loader2 size={13} className="spin" aria-hidden="true" /> : <Pause size={13} aria-hidden="true" />}
                {t('ui_control_pause')}
              </button>
            )}
            <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>
              {paused
                ? t('ui_control_paused_by', { who: pauseBy ?? '—', at: fmtTs(pauseAt ?? undefined) })
                : t('ui_control_pause_hint')}
            </span>
          </div>

          {error ? (
            <div style={{ fontSize: '0.75rem', color: 'var(--color-danger)', padding: '4px 2px' }}>
              {t('ui_control_error')}: {error}
            </div>
          ) : null}

          <div style={{ marginTop: 8 }}>
            <div style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)', marginBottom: 4 }}>
              {t('ui_control_audit')}
            </div>
            {ops && ops.length > 0 ? (
              <div className="ops-loglist" role="list" style={{ maxHeight: 200, overflowY: 'auto' }}>
                {ops.map((op, i) => (
                  <div
                    key={`${op.Kind}-${i}`}
                    className="ops-logrow"
                    role="listitem"
                    style={{ borderBottom: '1px solid var(--color-border)', padding: '4px 2px', display: 'flex', gap: 8, alignItems: 'baseline' }}
                    title={op.Reason || undefined}
                  >
                    <span style={{ fontSize: '0.75rem', fontWeight: 600 }}>{op.Kind}</span>
                    <span
                      className="ops-levelchip"
                      style={{
                        background: op.Status === 'COMPLETED' ? 'var(--color-success)' : 'var(--color-warning)',
                        color: 'var(--color-button-fg)',
                      }}
                    >
                      {op.Status}
                    </span>
                    <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>{op.RequestedBy}</span>
                    <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)', marginLeft: 'auto' }}>{fmtTs(op.CompletedAt || op.AcceptedAt)}</span>
                    {op.Reason ? (
                      <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>· {op.Reason}</span>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : (
              <div style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)', padding: '4px 2px' }}>
                {t('ui_control_no_ops')}
              </div>
            )}
          </div>
        </>
      )}

      <ConfirmDialog
        open={confirmPause}
        lang={lang}
        title={t('ui_control_pause_confirm')}
        confirmLabel={t('ui_yes')}
        cancelLabel={t('ui_cancel')}
        onConfirm={() => {
          setConfirmPause(false)
          void act('pause')
        }}
        onCancel={() => setConfirmPause(false)}
      />
    </div>
  )
}
