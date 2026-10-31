import { useCallback, useEffect, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  CloudOff,
  Cpu,
  Gauge,
  RefreshCw,
  Server,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import { api } from '../api'
import type { SetupReport } from '../types'
import { makeT, type UiLang } from '../locales/engine'

/**
 * System panel — the web twin of the mobile System screen and of `atf setup`'s
 * closing report. All three render `GET /api/system/setup`:
 *
 *   ready      the server's verdict, not a client guess
 *   services   live probes of core / logger / api / web with measured latency
 *   checks     the configuration contract, booleans only (never a secret value)
 *   features   what this build implements
 *   next_steps what to do when it is not ready
 *
 * Localisation follows the dashboard's convention: every string resolves
 * through the catalog (all ten locales), with English as the fallback.
 *
 * An older server without this endpoint answers 404: the panel then says so
 * instead of rendering an error the operator cannot act on.
 */
export function SystemPanel({ lang }: { lang: UiLang }) {
  const t = makeT(lang)
  const [report, setReport] = useState<SetupReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [unsupported, setUnsupported] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setUnsupported(false)
    try {
      setReport(await api.getSystemSetup())
    } catch (e: any) {
      const message = String(e?.message ?? e)
      // 404 / not-implemented means the server predates the endpoint.
      if (/40[34]|not.?found|not implemented/i.test(message)) {
        setUnsupported(true)
      } else {
        setError(message)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const ready = report?.ready ?? false
  const services = report?.services ?? []
  const online = services.filter((s) => s.online).length
  const contract = report?.checks ?? []
  const missing = report?.missing_required ?? []

  const levelLabel = (level: string) =>
    level === 'required'
      ? t('ui_required')
      : level === 'recommended'
        ? t('ui_recommended')
        : t('ui_optional')

  return (
    <section className="syspanel" aria-live="polite">
      <div className={`syspanel-hero syspanel-hero--${ready ? 'ready' : 'notready'}`}>
        <div className="syspanel-hero-main">
          <div className="syspanel-hero-title">
            {ready ? <ShieldCheck size={18} /> : <AlertTriangle size={18} />}
            <span>{ready ? t('ui_ready') : t('ui_not_ready')}</span>
            {report && (
              <span className="syspanel-verdict">
                {report.deployment || 'unknown'}
                {report.product_version ? ` · ${report.product_version}` : ''}
                {report.api_version ? ` · api ${report.api_version}` : ''}
              </span>
            )}
          </div>
          <div className="syspanel-hero-sub">
            {t('ui_server_readiness_service_topology_and_the_configuration_contract')}
          </div>
          <div className="syspanel-hero-stats">
            <span>
              <Server size={14} /> {t('ui_services_online')}{' '}
              <strong>
                {online}/{services.length}
              </strong>
            </span>
            <span>
              <Sparkles size={14} /> {t('ui_capabilities')}{' '}
              <strong>{report?.features.length ?? 0}</strong>
            </span>
            <span>
              <Gauge size={14} /> {t('ui_keys_set')}{' '}
              <strong>
                {contract.filter((c) => c.set).length}/{contract.length}
              </strong>
            </span>
            {report?.time && (
              <span className="syspanel-hero-time">
                <Activity size={14} /> {report.time}
              </span>
            )}
          </div>
        </div>
        <button className="btn syspanel-refresh" onClick={() => void load()} disabled={loading}>
          <RefreshCw size={15} className={loading ? 'spin' : undefined} />
          <span>{t('ui_re_probe')}</span>
        </button>
      </div>

      {missing.length > 0 && (
        <div className="syspanel-warning">
          <AlertTriangle size={15} />
          <span>
            {t('ui_missing_2_required_setting_s_missing', { missing_2: missing.length })}
          </span>
          <code>{missing.join(', ')}</code>
        </div>
      )}

      {unsupported && (
        <div className="syspanel-empty">
          <CloudOff size={16} />
          <span>
            {t('ui_this_server_does_not_expose_the_system_status_endpoint_update_th')}
          </span>
        </div>
      )}

      {error && (
        <div className="syspanel-warning">
          <AlertTriangle size={15} />
          <span>{error}</span>
        </div>
      )}

      {services.length > 0 && (
        <div className="syspanel-grid">
          {services.map((s) => (
            <div key={s.name} className="syspanel-card">
              <div className="syspanel-card-head">
                <Cpu size={15} color={s.online ? 'var(--success)' : 'var(--critical)'} />
                <span className="syspanel-card-name">{s.name.toUpperCase()}</span>
                <span className={`syspanel-dot syspanel-dot--${s.online ? 'up' : 'down'}`} />
              </div>
              <div className="syspanel-card-detail">{s.detail || '—'}</div>
              <div className="syspanel-card-foot">
                {s.online ? t('ui_system_online') : t('ui_system_offline')} · {s.latency_ms} ms
              </div>
            </div>
          ))}
        </div>
      )}

      {(report?.features.length ?? 0) > 0 && (
        <div className="syspanel-section">
          <h4>{t('ui_system_capabilities')}</h4>
          <div className="syspanel-chips">
            {report!.features.map((f) => (
              <span key={f} className="syspanel-chip">
                {f}
              </span>
            ))}
          </div>
        </div>
      )}

      {contract.length > 0 && (
        <div className="syspanel-section">
          <h4>{t('ui_configuration_contract')}</h4>
          <div className="syspanel-contract">
            {contract.map((c) => (
              <div key={c.key} className={`syspanel-check ${c.set ? 'is-set' : 'is-unset'}`}>
                <div className="syspanel-check-head">
                  <span className="syspanel-check-key">{c.key}</span>
                  <span className="syspanel-check-badge">
                    {levelLabel(c.level)} ·{' '}
                    {c.set ? t('ui_set') : t('ui_missing')}
                  </span>
                </div>
                {c.reason && <div className="syspanel-check-reason">{c.reason}</div>}
                {!c.set && c.hint && <div className="syspanel-check-hint">→ {c.hint}</div>}
              </div>
            ))}
          </div>
        </div>
      )}

      {(report?.next_steps.length ?? 0) > 0 && (
        <div className="syspanel-section">
          <h4>{t('ui_next_steps')}</h4>
          <ul className="syspanel-steps">
            {report!.next_steps.map((step) => (
              <li key={step}>
                <CheckCircle2 size={14} />
                <span>{step}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}
