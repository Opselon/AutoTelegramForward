import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Gauge, RefreshCw, ShieldAlert } from 'lucide-react'
import { api } from '../api'
import type { LogPolicyScopes, LogPolicyScope } from '../types'

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
            fontSize: '0.68rem',
            background: tone === 'effective' ? 'rgba(56,189,248,0.15)' : 'rgba(148,163,184,0.15)',
            color: tone === 'effective' ? '#38bdf8' : '#cbd5e1',
          }}
        >
          {l}
        </span>
      ))}
    </span>
  )
}

export function LogPolicyPanel({ lang = 'en' }: { lang?: string }) {
  const isRtl = lang === 'fa'
  const t = (fa: string, en: string) => (isRtl ? fa : en)

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
          ? t('تغییر اعمال و ذخیره شد (پس از راه‌اندازی مجدد باقی می‌ماند)', 'Applied and saved (survives a restart)')
          : t(
              'تغییر اعمال شد اما ذخیره نشد؛ پس از راه‌اندازی مجدد بازنشانی می‌شود',
              'Applied, but not saved: it will be reset by a restart',
            ),
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
        border: '1px solid rgba(255,255,255,0.08)',
        borderRadius: 10,
        padding: 12,
        background: 'rgba(15,23,42,0.5)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        {scope?.available ? (
          <CheckCircle2 size={15} color="#34d399" />
        ) : (
          <AlertTriangle size={15} color="#f59e0b" />
        )}
        <strong style={{ fontSize: '0.85rem' }}>{scope?.process ?? key}</strong>
        <span className="muted" style={{ fontSize: '0.7rem' }}>
          {scope?.scope}
        </span>
      </div>
      {!scope?.available ? (
        <div style={{ fontSize: '0.78rem', color: '#fbbf24' }}>
          {t('در دسترس نیست: ', 'Unavailable: ')}
          {scope?.reason || t('دلیل نامشخص', 'no reason given')}
        </div>
      ) : (
        <>
          <div style={{ fontSize: '0.78rem', marginBottom: 6 }}>
            <span className="muted">{t('سطح‌های تنظیم‌شده', 'Configured levels')}: </span>
            <Levels levels={scope.configured_levels} tone="configured" />
          </div>
          <div style={{ fontSize: '0.78rem', marginBottom: 6 }}>
            <span className="muted">{t('در حال جمع‌آوری', 'Collecting now')}: </span>
            <Levels levels={scope.effective_levels} tone="effective" />
          </div>
          <div style={{ fontSize: '0.78rem', marginBottom: 6 }}>
            <span className="muted">{t('سقف نرخ', 'Rate ceiling')}: </span>
            {scope.verbose_rate_per_second
              ? t(`${scope.verbose_rate_per_second} رویداد در ثانیه`, `${scope.verbose_rate_per_second} events/s`)
              : t('بدون محدودیت', 'unlimited')}
          </div>
          {scope.diagnostic_active && (
            <div style={{ fontSize: '0.78rem', color: '#fbbf24', marginBottom: 6 }}>
              {t('حالت تشخیصی فعال است', 'Diagnostic window active')} ·{' '}
              {scope.diagnostic_seconds_remaining
                ? t(`${scope.diagnostic_seconds_remaining} ثانیه باقی‌مانده`, `${scope.diagnostic_seconds_remaining}s left`)
                : new Date((scope.diagnostic_until ?? 0) * 1000).toISOString()}
            </div>
          )}
          {scope.counters && (
            <div className="muted" style={{ fontSize: '0.72rem' }}>
              {t(
                `${scope.counters.emitted} ارسال‌شده · ${scope.counters.suppressed_level} حذف‌شده با سطح · ${scope.counters.suppressed_rate} حذف‌شده با نرخ`,
                `${scope.counters.emitted} emitted · ${scope.counters.suppressed_level} dropped by level · ${scope.counters.suppressed_rate} dropped by rate`,
              )}
            </div>
          )}
        </>
      )}
    </div>
  )

  return (
    <section className="glass-panel" style={{ padding: 18, marginBottom: 20 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
        <h3 style={{ fontSize: '1rem', fontWeight: 700, display: 'flex', alignItems: 'center', gap: 8 }}>
          <Gauge size={17} color="#38bdf8" />
          <span>{t('سیاست جمع‌آوری لاگ (هر سرویس)', 'Log Collection Policy (per service)')}</span>
        </h3>
        <button
          onClick={() => void load()}
          className="btn btn-secondary"
          style={{ padding: '6px 12px', fontSize: '0.8rem' }}
          disabled={loading}
        >
          <RefreshCw size={14} />
          <span>{t('بروزرسانی', 'Refresh')}</span>
        </button>
      </div>

      <p className="muted" style={{ fontSize: '0.78rem', margin: '8px 0 14px' }}>
        {t(
          'سیاست برای هر فرایند جداگانه اعمال می‌شود؛ اعداد و سطوح زیر همان چیزی است که خود سرویس گزارش می‌دهد. تغییر فقط توسط ادمین و از طریق هسته اعمال می‌شود.',
          'The policy is enforced per process, so each card is that process’s own answer. Changes are admin-only and are applied by the core that enforces them.',
        )}
      </p>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 12 }}>
        {scopes ? Object.entries(scopes).map(([k, v]) => scopeCard(k, v)) : null}
      </div>

      <div
        style={{
          marginTop: 16,
          borderTop: '1px solid rgba(255,255,255,0.08)',
          paddingTop: 14,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
          <ShieldAlert size={15} color="#f59e0b" />
          <strong style={{ fontSize: '0.85rem' }}>
            {t('تغییر سیاست هسته (atf-core)', 'Change core policy (atf-core)')}
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
                  fontSize: '0.7rem',
                  border: on ? '1px solid rgba(56,189,248,0.5)' : '1px solid rgba(255,255,255,0.12)',
                  background: on ? 'rgba(56,189,248,0.18)' : 'rgba(15,23,42,0.6)',
                  color: on ? '#38bdf8' : '#94a3b8',
                }}
                title={t('برای تغییر کلیک کنید', 'Click to toggle')}
              >
                {level}
              </button>
            )
          })}
        </div>

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'flex-end' }}>
          <label style={{ fontSize: '0.75rem', display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="muted">{t('سقف نرخ (۰ = بدون محدودیت)', 'Rate ceiling (0 = unlimited)')}</span>
            <input
              type="number"
              min={0}
              value={rate}
              onChange={(e) => setRate(e.target.value)}
              style={{ width: 120 }}
            />
          </label>
          <label style={{ fontSize: '0.75rem', display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="muted">{t('پنجره تشخیصی (ثانیه)', 'Diagnostic window (seconds)')}</span>
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
            onClick={() =>
              void apply({
                levels,
                verbose_rate_per_second: Number(rate) || 0,
                diagnostic_seconds: Number(seconds) || 0,
              })
            }
          >
            {t('اعمال سطح‌ها و نرخ', 'Apply levels and rate')}
          </button>
          <button
            className="btn btn-secondary"
            disabled={saving || !core?.available}
            onClick={() => void apply({ disable_diagnostics: true })}
          >
            {t('پایان حالت تشخیصی', 'Stop diagnostics')}
          </button>
        </div>

        {levels.length === 0 && (
          <div style={{ fontSize: '0.75rem', color: '#fbbf24', marginTop: 8 }}>
            {t('حداقل یک سطح باید انتخاب شود', 'At least one level must be selected')}
          </div>
        )}

        {error && (
          <div
            style={{
              marginTop: 10,
              fontSize: '0.78rem',
              color: '#f87171',
              background: 'rgba(239,68,68,0.1)',
              border: '1px solid rgba(239,68,68,0.35)',
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
              color: '#34d399',
              background: 'rgba(16,185,129,0.1)',
              border: '1px solid rgba(16,185,129,0.35)',
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
