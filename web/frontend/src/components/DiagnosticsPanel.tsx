import { ShieldAlert, AlertTriangle, Info, CheckCircle2, RefreshCw } from 'lucide-react'
import type { DiagnosticsResponse, DiagnosticFinding } from '../types'
import { makeT, type UiLang } from '../locales/engine'

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
  lang,
}: {
  report: DiagnosticsResponse | null
  loading: boolean
  onRefresh: () => void
  lang: UiLang
}) {
  const t = makeT(lang)

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
            <span>{t('ui_system_preflight')}</span>
            {report && <span className="preflight-score">· {report.score}/100</span>}
          </div>
          <div className="preflight-meta">
            {report
              ? t('ui_report_4_errors_report_5_warnings_report_6_info', { report_4: report.counts.error, report_5: report.counts.warning, report_6: report.counts.info })
              : t('ui_scanning')}
            {report?.scanned ? (
              <>
                {' — '}
                {t('ui_checked_report_3_tasks_report_4_ai_configs', { report_3: report.scanned.rules, report_4: report.scanned.ai_configs })}
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
          {t('ui_re_run_check')}
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
          {t('ui_no_problems_found_every_task_is_healthy')}
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
