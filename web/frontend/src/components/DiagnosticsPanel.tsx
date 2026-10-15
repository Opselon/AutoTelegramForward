import { ShieldAlert, AlertTriangle, Info, CheckCircle2, RefreshCw } from 'lucide-react'
import type { DiagnosticsResponse, DiagnosticFinding } from '../types'

/**
 * Preflight diagnostics panel — renders the /api/diagnostics report as ranked,
 * actionable cards so a misconfiguration (an AI config whose provider is
 * unreachable or deleted, a task looping onto itself, a dead session) is caught
 * before it silently swallows traffic instead of being discovered later from
 * gaps in the output.
 *
 * Styling lives in index.css (`.preflight`, `.finding`, `.preflight-chip`) so
 * severity colours stay defined in one place alongside the rest of the look.
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

  const sevIcon = (s: string) =>
    s === 'error' ? (
      <ShieldAlert size={15} />
    ) : s === 'warning' ? (
      <AlertTriangle size={15} />
    ) : (
      <Info size={15} />
    )

  const findings: DiagnosticFinding[] = report?.findings ?? []
  const status = report?.status ?? 'healthy'

  return (
    <section className={`preflight preflight--${status}`} aria-live="polite">
      <div className="preflight-head">
        <div>
          <div className="preflight-title">
            {status === 'healthy' ? (
              <CheckCircle2 size={18} color="var(--success)" />
            ) : (
              <ShieldAlert
                size={18}
                color={status === 'critical' ? 'var(--critical)' : 'var(--warning)'}
              />
            )}
            <span>{t('بررسی پیش‌پرواز سیستم', 'System Preflight')}</span>
            {report && <span className="preflight-score">· {report.score}/100</span>}
          </div>
          <div className="preflight-meta">
            {report
              ? t(
                  `${report.counts.error} خطا · ${report.counts.warning} هشدار · ${report.counts.info} اطلاع`,
                  `${report.counts.error} errors · ${report.counts.warning} warnings · ${report.counts.info} info`,
                )
              : t('در حال بررسی…', 'Scanning…')}
            {report?.scanned ? (
              <>
                {' — '}
                {t(
                  `${report.scanned.rules} تسک و ${report.scanned.ai_configs} پیکربندی AI بررسی شد`,
                  `checked ${report.scanned.rules} tasks, ${report.scanned.ai_configs} AI configs`,
                )}
              </>
            ) : null}
          </div>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="btn btn-secondary"
          style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: '0.78rem' }}
        >
          <RefreshCw size={14} className={loading ? 'spin' : undefined} />
          {t('بررسی مجدد', 'Re-run check')}
        </button>
      </div>

      {report && report.services?.length > 0 && (
        <div className="preflight-services">
          {report.services.map((s) => (
            <span
              key={s.name}
              title={s.detail || ''}
              className={`preflight-chip preflight-chip--${s.online ? 'up' : 'down'}`}
            >
              {s.online ? '●' : '○'} {s.name}
            </span>
          ))}
        </div>
      )}

      {report && findings.length === 0 ? (
        <p className="preflight-ok">
          {t(
            'هیچ مشکلی پیدا نشد — همه تسک‌ها سالم هستند.',
            'No problems found — every task is healthy.',
          )}
        </p>
      ) : (
        <div className="preflight-findings">
          {findings.map((f) => (
            <div key={f.id} className={`finding finding--${f.severity}`}>
              <div className="finding-title">
                {sevIcon(f.severity)}
                <span>{f.title}</span>
              </div>
              <div className="finding-detail">{f.detail}</div>
              <div className="finding-fix">💡 {f.remediation}</div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
