import { ShieldAlert, AlertTriangle, Info, CheckCircle2, RefreshCw } from 'lucide-react'
import type { DiagnosticsResponse, DiagnosticFinding } from '../types'

/**
 * Preflight diagnostics panel — renders the /api/diagnostics report as ranked,
 * actionable cards so a misconfiguration (AI rewrite pointing at a deleted
 * config, a task looping onto itself, a dead session) is caught before it
 * silently swallows traffic instead of being discovered later from gaps.
 */
export function DiagnosticsPanel({
  report,
  loading,
  onRefresh,
  isRtl,
}: {
  report: DiagnosticsResponse | null
  loading: boolean
  onRefresh: () => void
  isRtl: boolean
}) {
  const t = (fa: string, en: string) => (isRtl ? fa : en)

  const statusColor = (s?: string) =>
    s === 'critical' ? '#f87171' : s === 'warning' ? '#fbbf24' : '#34d399'
  const sevColor = (s: string) =>
    s === 'error' ? '#f87171' : s === 'warning' ? '#fbbf24' : '#60a5fa'
  const sevIcon = (s: string) =>
    s === 'error' ? <ShieldAlert size={15} /> : s === 'warning' ? <AlertTriangle size={15} /> : <Info size={15} />

  const findings: DiagnosticFinding[] = report?.findings ?? []
  const color = statusColor(report?.status)

  return (
    <div
      style={{
        background: 'rgba(17, 24, 39, 0.72)',
        border: `1px solid ${color}44`,
        borderRadius: 14,
        padding: 16,
        marginBottom: 16,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {report?.status === 'healthy' ? (
            <CheckCircle2 size={18} color={color} />
          ) : (
            <ShieldAlert size={18} color={color} />
          )}
          <div>
            <div style={{ fontWeight: 800, fontSize: '0.92rem', color }}>
              {t('بررسی پیش‌پرواز سیستم', 'System Preflight')}
              {report ? ` · ${report.score}/100` : ''}
            </div>
            <div style={{ fontSize: '0.72rem', color: '#94a3b8' }}>
              {report
                ? t(
                    `${report.counts.error} خطا · ${report.counts.warning} هشدار · ${report.counts.info} اطلاع`,
                    `${report.counts.error} errors · ${report.counts.warning} warnings · ${report.counts.info} info`,
                  )
                : t('در حال بررسی…', 'Scanning…')}
            </div>
          </div>
        </div>
        <button
          onClick={onRefresh}
          disabled={loading}
          className="btn btn-ghost"
          style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.78rem' }}
        >
          <RefreshCw size={14} className={loading ? 'spin' : undefined} />
          {t('بررسی مجدد', 'Re-run check')}
        </button>
      </div>

      {report && report.services?.length > 0 && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 12 }}>
          {report.services.map((s) => (
            <span
              key={s.name}
              title={s.detail || ''}
              style={{
                fontSize: '0.7rem',
                padding: '3px 9px',
                borderRadius: 999,
                background: s.online ? 'rgba(16,185,129,0.14)' : 'rgba(248,113,113,0.16)',
                color: s.online ? '#34d399' : '#f87171',
                border: `1px solid ${s.online ? 'rgba(16,185,129,0.3)' : 'rgba(248,113,113,0.35)'}`,
              }}
            >
              {s.online ? '●' : '○'} {s.name}
            </span>
          ))}
        </div>
      )}

      {findings.length === 0 && report ? (
        <div style={{ marginTop: 12, fontSize: '0.8rem', color: '#34d399' }}>
          {t('هیچ مشکلی پیدا نشد — همه تسک‌ها سالم هستند.', 'No problems found — every task is healthy.')}
        </div>
      ) : (
        <div style={{ marginTop: 12, display: 'grid', gap: 8 }}>
          {findings.map((f) => (
            <div
              key={f.id}
              style={{
                borderLeft: `3px solid ${sevColor(f.severity)}`,
                background: 'rgba(2, 6, 23, 0.5)',
                borderRadius: 8,
                padding: '9px 12px',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 7, color: sevColor(f.severity), fontWeight: 700, fontSize: '0.82rem' }}>
                {sevIcon(f.severity)}
                <span>{f.title}</span>
              </div>
              <div style={{ fontSize: '0.75rem', color: '#cbd5e1', marginTop: 4 }}>{f.detail}</div>
              <div style={{ fontSize: '0.72rem', color: '#94a3b8', marginTop: 3 }}>
                💡 {f.remediation}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
