import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Gauge, RefreshCw, ShieldAlert } from 'lucide-react'
import { api } from '../api'
import type { LogPolicyScopes, LogPolicyScope } from '../types'
import { makeT, type UiLang } from '../locales/engine'

/**
 * Operator control for the collection policy, per producer.
 *
 * Two things make this panel different from a settings form, and both are shown
 * rather than assumed: the policy is enforced *per process*, so every number is
 * labelled with the process that reported it, and the answer after a change is
 * read back from that process rather than echoed from the form. A change that the
 * producer accepted but could not persist says so, because "in force until the
 * next restart" and "saved" are different promises.
 */

const ALL_LEVELS = ['TRACE', 'DEBUG', 'INFO', 'SUCCESS', 'WARN', 'ERROR', 'CRITICAL']

/** A refusal comes back as JSON in the error body; show the reason, not the blob. */
function messageOf(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed.error === 'string') return parsed.error
  } catch {
    /* not JSON: the raw text is the message */
  }
  return raw
}

function Levels({
  levels,
  tone,
}: {
  levels: string[] | undefined
  tone: 'configured' | 'effective'
}) {
  if (!levels?.length) return <span className="muted">—</span>
  return (
    <span style={{ display: 'inline-flex', flexWrap: 'wrap', gap: 4 }}>
      {levels.map((l) => (
        <span
          key={l}
          className="badge"
          style={{
            fontSize: 'var(--type-xs)',
            background: tone === 'effective' ? 'var(--color-info-soft)' : 'var(--color-hover-wash)',
            color: tone === 'effective' ? 'var(--color-info)' : 'var(--color-text)',
          }}
        >
          {l}
        </span>
      ))}
    </span>
  )
}

export function LogPolicyPanel({ lang = 'en' }: { lang?: UiLang }) {
  const t = makeT(lang)

  const [scopes, setScopes] = useState<LogPolicyScopes | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const core = scopes?.core
  const [levels, setLevels] = useState<string[]>([])
  const [rate, setRate] = useState<string>('0')
  const [seconds, setSeconds] = useState<string>('600')

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.getLogPolicy()
      setScopes(res.scopes)
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // Seed the form from the policy the core reports it is enforcing, so the form
  // starts where the process actually is rather than at a hard-coded default.
  useEffect(() => {
    if (!core?.available) return
    setLevels(core.configured_levels ?? [])
    setRate(String(core.verbose_rate_per_second ?? 0))
  }, [core?.available, core?.configured_levels, core?.verbose_rate_per_second])

  const toggle = (level: string) =>
    setLevels((prev) =>
      prev.includes(level) ? prev.filter((l) => l !== level) : [...prev, level],
    )

  const apply = async (body: Parameters<typeof api.updateLogPolicy>[0]) => {
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const res = await api.updateLogPolicy(body)
      // The response carries the state read back from the process, so the panel
      // shows what is enforced now, not what was submitted.
      setScopes((prev) => (prev ? { ...prev, core: res.scope } : prev))
      setNotice(
        res.persisted
          ? t('ui_applied_and_saved_survives_a_restart')
          : t('ui_applied_but_not_saved_it_will_be_reset_by_a_restart'),
      )
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setSaving(false)
    }
  }

  const scopeCard = (key: string, scope: LogPolicyScope | undefined) => (
    <div
      key={key}
      style={{
        border: '1px solid var(--color-border)',
        borderRadius: 10,
        padding: 12,
        background: 'var(--color-surface-2)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        {scope?.available ? (
          <CheckCircle2 size={15} color="var(--color-success)" />
        ) : (
          <AlertTriangle size={15} color="var(--color-warning)" />
        )}
        <strong style={{ fontSize: '0.85rem' }}>{scope?.process ?? key}</strong>
        <span className="muted" style={{ fontSize: 'var(--type-xs)' }}>
          {scope?.scope}
        </span>
      </div>
      {!scope?.available ? (
        <div style={{ fontSize: '0.78rem', color: 'var(--color-warning)' }}>
          {t('ui_unavailable')}
          {scope?.reason || t('ui_no_reason_given')}
        </div>
      ) : (
        <>
          <div style={{ fontSize: '0.78rem', marginBottom: 6 }}>
            <span className="muted">{t('ui_configured_levels')}: </span>
            <Levels levels={scope.configured_levels} tone="configured" />
          </div>
          <div style={{ fontSize: '0.78rem', marginBottom: 6 }}>
            <span className="muted">{t('ui_collecting_now')}: </span>
            <Levels levels={scope.effective_levels} tone="effective" />
          </div>
          <div style={{ fontSize: '0.78rem', marginBottom: 6 }}>
            <span className="muted">{t('ui_rate_ceiling')}: </span>
            {scope.verbose_rate_per_second
              ? t('ui_scope_2_events_s', { scope_2: scope.verbose_rate_per_second })
              : t('ui_unlimited')}
          </div>
          {scope.diagnostic_active && (
            <div style={{ fontSize: '0.78rem', color: 'var(--color-warning)', marginBottom: 6 }}>
              {t('ui_diagnostic_window_active')} ·{' '}
              {scope.diagnostic_seconds_remaining
                ? t('ui_scope_2_s_left', { scope_2: scope.diagnostic_seconds_remaining })
                : new Date((scope.diagnostic_until ?? 0) * 1000).toISOString()}
            </div>
          )}
          {scope.counters && (
            <div className="muted" style={{ fontSize: 'var(--type-xs)' }}>
              {t('ui_scope_4_emitted_scope_5_dropped_by_level_scope_6_dropped_by_rate', { scope_4: scope.counters.emitted, scope_5: scope.counters.suppressed_level, scope_6: scope.counters.suppressed_rate })}
            </div>
          )}
        </>
      )}
    </div>
  )

  return (
    <section className="glass-panel" style={{ padding: 18, marginBottom: 20 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
        <h3 style={{ fontSize: 'var(--type-md)', fontWeight: 700, display: 'flex', alignItems: 'center', gap: 8 }}>
          <Gauge size={17} color="var(--color-info)" />
          <span>{t('ui_log_collection_policy_per_service')}</span>
        </h3>
        <button
          onClick={() => void load()}
          className="btn btn-secondary"
          style={{ padding: '6px 12px', fontSize: '0.8rem' }}
          disabled={loading}
        >
          <RefreshCw size={14} />
          <span>{t('ui_refresh')}</span>
        </button>
      </div>

      <p className="muted" style={{ fontSize: '0.78rem', margin: '8px 0 14px' }}>
        {t('ui_the_policy_is_enforced_per_process_so_each_card_is_that_process')}
      </p>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 12 }}>
        {scopes ? Object.entries(scopes).map(([k, v]) => scopeCard(k, v)) : null}
      </div>

      {/* Collected ≠ stored. The engine is the second gate: a level the
          producers admit but the engine omits is counted as filtered, so the
          card that says that has to sit where the operator already looks
          (issue #102). */}
      {scopes?.engine?.available && (
        <div
          style={{
            marginTop: 12,
            padding: '10px 12px',
            borderRadius: 10,
            border:
              scopes.engine.effective_levels && scopes.engine.effective_levels.length > 0
                ? '1px solid color-mix(in oklab, var(--color-warning) 35%, transparent)'
                : '1px solid color-mix(in oklab, var(--color-success) 35%, transparent)',
            background:
              scopes.engine.effective_levels && scopes.engine.effective_levels.length > 0
                ? 'var(--color-warning-soft)'
                : 'var(--color-success-soft)',
            fontSize: '0.78rem',
          }}
        >
          <strong>{t('ui_engine_admission_title')}</strong>
          <div className="muted" style={{ marginTop: 4 }}>
            {t('ui_engine_admission_explain')}
          </div>
          {scopes.engine.effective_levels && scopes.engine.effective_levels.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <span className="muted">{t('ui_engine_omitted_levels')}: </span>
              <Levels levels={scopes.engine.effective_levels} tone="configured" />
            </div>
          )}
          {scopes.engine.counters && Number(scopes.engine.counters.suppressed_level) > 0 && (
            <div className="muted" style={{ marginTop: 4 }}>
              {t('ui_engine_filtered_total', {
                count: scopes.engine.counters.suppressed_level,
              })}
            </div>
          )}
        </div>
      )}

      <div
        style={{
          marginTop: 16,
          borderTop: '1px solid var(--color-border)',
          paddingTop: 14,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
          <ShieldAlert size={15} color="var(--color-warning)" />
          <strong style={{ fontSize: '0.85rem' }}>
            {t('ui_change_core_policy_atf_core')}
          </strong>
        </div>

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
          {ALL_LEVELS.map((level) => {
            const on = levels.includes(level)
            return (
              <button
                key={level}
                onClick={() => toggle(level)}
                className="badge"
                style={{
                  cursor: 'pointer',
                  fontSize: 'var(--type-xs)',
                  border: on ? '1px solid color-mix(in oklab, var(--color-info) 50%, transparent)' : '1px solid var(--color-border)',
                  background: on ? 'var(--color-info-soft)' : 'var(--color-surface-2)',
                  color: on ? 'var(--color-info)' : 'var(--color-text-muted)',
                }}
                title={t('ui_click_to_toggle')}
              >
                {level}
              </button>
            )
          })}
        </div>

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'flex-end' }}>
          <label style={{ fontSize: 'var(--type-xs)', display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="muted">{t('ui_rate_ceiling_0_unlimited')}</span>
            <input
              type="number"
              min={0}
              value={rate}
              onChange={(e) => setRate(e.target.value)}
              style={{ width: 120 }}
            />
          </label>
          <label style={{ fontSize: 'var(--type-xs)', display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="muted">{t('ui_diagnostic_window_seconds')}</span>
            <input
              type="number"
              min={1}
              max={3600}
              value={seconds}
              onChange={(e) => setSeconds(e.target.value)}
              style={{ width: 120 }}
            />
          </label>

          <button
            className="btn btn-primary"
            disabled={saving || !core?.available || levels.length === 0}
            onClick={() => {
              // Both fields are always stated: an explicit 0 for the ceiling is
              // the operator asking for no limit, so it must reach the API as
              // 0 rather than as an absent field or a silent default.
              const parsedRate = Number(rate)
              const parsedSeconds = Number(seconds)
              if (!Number.isFinite(parsedRate) || parsedRate < 0) {
                setError(
                  t('ui_the_rate_ceiling_must_be_a_non_negative_number_0_unlimited'),
                )
                return
              }
              if (!Number.isFinite(parsedSeconds) || parsedSeconds < 1 || parsedSeconds > 3600) {
                setError(
                  t('ui_the_diagnostic_window_must_be_between_1_and_3600_seconds'),
                )
                return
              }
              void apply({
                levels,
                verbose_rate_per_second: parsedRate,
                diagnostic_seconds: parsedSeconds,
              })
            }}
          >
            {t('ui_apply_levels_and_rate')}
          </button>
          <button
            className="btn btn-secondary"
            disabled={saving || !core?.available}
            onClick={() => void apply({ disable_diagnostics: true })}
          >
            {t('ui_stop_diagnostics')}
          </button>
        </div>

        {levels.length === 0 && (
          <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-warning)', marginTop: 8 }}>
            {t('ui_at_least_one_level_must_be_selected')}
          </div>
        )}

        {error && (
          <div
            style={{
              marginTop: 10,
              fontSize: '0.78rem',
              color: 'var(--color-danger)',
              background: 'var(--color-danger-soft)',
              border: '1px solid color-mix(in oklab, var(--color-danger) 35%, transparent)',
              borderRadius: 8,
              padding: '8px 10px',
            }}
          >
            {error}
          </div>
        )}
        {notice && (
          <div
            style={{
              marginTop: 10,
              fontSize: '0.78rem',
              color: 'var(--color-success)',
              background: 'var(--color-success-soft)',
              border: '1px solid color-mix(in oklab, var(--color-success) 35%, transparent)',
              borderRadius: 8,
              padding: '8px 10px',
            }}
          >
            {notice}
          </div>
        )}
      </div>
    </section>
  )
}
