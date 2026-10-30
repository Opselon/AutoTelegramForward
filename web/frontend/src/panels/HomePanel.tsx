/**
 * Home — the landing tab.
 *
 * The shell used to drop the operator straight into a raw readiness dump
 * (SystemPanel), which answers "is the server configured" but not "what do I do
 * from here". This panel is the dashboard's front door:
 *
 * - a hero row that names the user and shows the same live health dots the
 *   header chips probe (core / logger / web);
 * - quick actions that open the four surfaces people actually need first —
 *   the rule editor, the rules list, the live logs and the queue;
 * - the four KPI cards (moved here from the shell, where they sat above every
 *   tab and stole a screen row from panels that never used them);
 * - a system-readiness card summarizing `GET /api/system/setup`, with the full
 *   SystemPanel one click away for the operator who wants every check;
 * - a quiet, dismissible teaser of the latest ERROR/WARN log events.
 *
 * Everything reads from the shared dashboard context — this panel owns no
 * polling of its own except the two one-shot fetches (setup report, recent
 * alerts), so Home never doubles the traffic the shell already generates.
 */
import { lazy, Suspense, useEffect, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Layers,
  ListTodo,
  Plus,
  Radio,
  Send,
  Server,
  Users,
  X,
} from 'lucide-react'
import { api } from '../api'
import type { LogItem, SetupReport } from '../types'
import { Skeleton, SkeletonRows } from '../components/ui/Primitives'
import type { DashboardCtx } from '../App'

// The full readiness dump stays a chunk of its own: it is only downloaded when
// the operator actually opens the details card.
const SystemPanel = lazy(() =>
  import('../components/SystemPanel').then((m) => ({ default: m.SystemPanel })),
)

export function HomePanel({ d }: { d: DashboardCtx }) {
  const {
    t,
    lang,
    currentUser,
    sessions,
    stats,
    gatewayInfo,
    dlqJobs,
    openRuleModal,
    setActiveTab,
  } = d

  // ---- hero: the friendliest name we have for this operator ----------------
  // me().username wins; the bot-linked session's name is the fallback for
  // accounts that never set a dashboard username.
  const name =
    currentUser?.username ||
    sessions.find((s) => s.first_name)?.first_name ||
    sessions[0]?.username ||
    'ATF'

  // ---- system readiness (one-shot; SystemPanel re-probes on demand) --------
  const [setup, setSetup] = useState<SetupReport | null>(null)
  const [setupUnsupported, setSetupUnsupported] = useState(false)
  const [showDetails, setShowDetails] = useState(false)
  useEffect(() => {
    let alive = true
    api
      .getSystemSetup()
      .then((report) => {
        if (alive) setSetup(report)
      })
      .catch((e: unknown) => {
        // An older server without the endpoint is a *quiet* miss here: the
        // details card (SystemPanel) explains it when opened.
        if (/40[34]|not.?found|not implemented/i.test(String((e as Error)?.message ?? e))) {
          if (alive) setSetupUnsupported(true)
        }
      })
    return () => {
      alive = false
    }
  }, [])

  const failedChecks = setup ? setup.checks.filter((c) => !c.set).length : 0

  // ---- recent activity: the newest ERROR/WARN events, one-shot -------------
  const [recentAlerts, setRecentAlerts] = useState<LogItem[] | null>(null)
  const [alertsDismissed, setAlertsDismissed] = useState(false)
  useEffect(() => {
    let alive = true
    api
      .getLogsPage({ levels: ['ERROR', 'WARN'], limit: 5, ascending: false })
      .then((page) => {
        if (alive) setRecentAlerts(Array.isArray(page?.logs) ? page.logs : [])
      })
      .catch(() => {
        // A failed teaser is not an incident: stay quiet, show the empty state.
        if (alive) setRecentAlerts([])
      })
    return () => {
      alive = false
    }
  }, [])

  const healthChips = [
    { label: 'Core:50051', online: stats?.atf_core_online },
    { label: 'Logger:50052', online: stats?.atf_logger_online },
    { label: 'Web:8088', online: gatewayInfo?.atf_web_online },
  ] as const

  // A chip must say what it means. The dot alone turned "not probed yet"
  // (undefined on first paint) into the same red as a real outage, and a real
  // outage named no service — the operator had to open the DevOps panel to
  // find out which one. Verdict text is the same catalog key the System panel
  // uses, so all three surfaces agree word for word.
  const healthVerdict = (online: boolean | undefined) =>
    online === undefined
      ? t('ui_probing')
      : online
        ? t('ui_system_online')
        : t('ui_system_offline')

  // The four KPIs, moved verbatim from the shell (same sources, same tones).
  const kpis = [
    {
      icon: <Layers size={20} aria-hidden="true" />,
      label: t('ui_active_rules'),
      value: stats
        ? `${(stats.active_rules ?? 0).toLocaleString()} / ${(stats.total_rules ?? 0).toLocaleString()}`
        : null,
      tone: 'primary',
    },
    {
      icon: <Send size={20} aria-hidden="true" />,
      label: t('ui_forwarded_24h'),
      value: stats ? (stats.total_forwarded_24h ?? 0).toLocaleString() : null,
      tone: 'warning',
    },
    {
      icon: <Users size={20} aria-hidden="true" />,
      label: t('ui_connected_sessions'),
      value: stats
        ? `${(stats.active_sessions ?? 0).toLocaleString()} / ${(stats.total_sessions ?? 0).toLocaleString()}`
        : null,
      tone: 'success',
    },
    {
      icon: <Activity size={20} aria-hidden="true" />,
      label: t('ui_dead_letter_queue'),
      value: dlqJobs.length.toLocaleString(),
      tone: dlqJobs.length > 0 ? 'danger' : 'neutral',
    },
  ] as const

  const quickActions = [
    {
      icon: <Plus size={18} aria-hidden="true" />,
      label: t('ui_new_forwarding_task'),
      onClick: () => openRuleModal(null),
      tone: 'home-quick__tile--primary',
    },
    {
      icon: <Layers size={18} aria-hidden="true" />,
      label: t('ui_open_rules'),
      onClick: () => setActiveTab('rules'),
      tone: '',
    },
    {
      icon: <Server size={18} aria-hidden="true" />,
      label: t('ui_live_logs'),
      onClick: () => setActiveTab('devops'),
      tone: '',
    },
    {
      icon: <ListTodo size={18} aria-hidden="true" />,
      label: t('ui_queue_dlq'),
      onClick: () => setActiveTab('queue'),
      tone: '',
    },
  ] as const

  return (
    <div className="home-panel">
      {/* Hero: greeting, product version, the real service-health dots. */}
      <section className="glass-panel home-hero" aria-live="polite">
        <div className="home-hero__id">
          <span className="home-hero__mark" aria-hidden="true">
            <Radio size={20} />
          </span>
          <div className="home-hero__text">
            <h2 className="home-hero__greeting">{t('ui_home_greeting', { name })}</h2>
            <div className="home-hero__meta">
              <span className="badge badge--info">
                {gatewayInfo?.version ? `v${gatewayInfo.version}` : t('ui_version_probing')}
              </span>
              <span className="home-hero__tagline">
                {t('ui_unified_smart_telegram_forwarding_gateway_microservice_platform')}
              </span>
            </div>
          </div>
        </div>
        <div className="home-hero__health" role="status">
          {healthChips.map((chip) => (
            <span
              key={chip.label}
              className={`header-chip${chip.online === false ? ' header-chip--down' : ''}`}
              title={`${chip.label} — ${healthVerdict(chip.online)}`}
            >
              <span
                className={`header-chip__dot${chip.online ? ' is-online' : ''}${chip.online === undefined ? ' is-unknown' : ''}`}
                aria-hidden="true"
              />
              <span>{chip.label}</span>
              <span className="header-chip__verdict">{healthVerdict(chip.online)}</span>
            </span>
          ))}
        </div>
      </section>

      {/* Quick actions: the four places a session usually starts. */}
      <nav className="home-quick" aria-label={t('ui_quick_actions')}>
        {quickActions.map((action) => (
          <button
            key={action.label}
            type="button"
            className={`home-quick__tile ${action.tone}`.trim()}
            onClick={action.onClick}
          >
            <span className="home-quick__icon" aria-hidden="true">
              {action.icon}
            </span>
            <span className="home-quick__label">{action.label}</span>
          </button>
        ))}
      </nav>

      {/* KPI cards — the same four the shell used to render above every tab. */}
      <div className="home-kpi-grid">
        {kpis.map((card) => (
          <div key={card.label} className="glass-panel overview-card">
            <span className={`overview-card__icon overview-card__icon--${card.tone}`}>
              {card.icon}
            </span>
            <div className="overview-card__text">
              <div className="overview-card__label">{card.label}</div>
              {card.value === null ? (
                <Skeleton height={20} width="60%" />
              ) : (
                <div className="overview-card__value">{card.value}</div>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="home-cols">
        {/* System readiness: the server's own verdict, summarized. */}
        <section className="card home-readiness">
          <header className="card__head">
            <h3 className="card__title">{t('ui_system_readiness')}</h3>
            {setup && (
              <span className={`badge ${setup.ready ? 'badge--success' : 'badge--warning'}`}>
                {setup.ready ? t('ui_ready') : t('ui_not_ready')}
              </span>
            )}
          </header>
          <div className="card__body">
            {setup ? (
              <p className="home-readiness__summary">
                {failedChecks > 0
                  ? t('ui_checks_failing', { count: failedChecks })
                  : t('ui_all_checks_set')}
              </p>
            ) : (
              <p className="home-readiness__summary">
                {setupUnsupported ? t('ui_empty_body') : t('ui_panel_loading')}
              </p>
            )}
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              aria-expanded={showDetails}
              onClick={() => setShowDetails((v) => !v)}
            >
              {t('ui_view_full_details')}
            </button>
            {showDetails && (
              <Suspense fallback={<SkeletonRows rows={4} lang={lang} />}>
                <SystemPanel lang={lang} />
              </Suspense>
            )}
          </div>
        </section>

        {/* Recent activity: the newest ERROR/WARN events, dismissible. */}
        {!alertsDismissed && (
          <section className="card home-activity">
            <header className="card__head">
              <h3 className="card__title">{t('ui_recent_activity')}</h3>
              <div className="card__action">
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  onClick={() => setActiveTab('devops')}
                >
                  {t('ui_view_all_logs')}
                </button>
                <button
                  type="button"
                  className="btn btn--ghost btn--icon btn--sm home-activity__dismiss"
                  aria-label={t('ui_dismiss')}
                  title={t('ui_dismiss')}
                  onClick={() => setAlertsDismissed(true)}
                >
                  <X size={14} aria-hidden="true" />
                </button>
              </div>
            </header>
            <div className="card__body">
              {recentAlerts === null ? (
                <SkeletonRows rows={3} lang={lang} />
              ) : recentAlerts.length === 0 ? (
                <div className="empty-state">
                  <span className="empty-state__icon" aria-hidden="true">
                    <CheckCircle2 size={22} />
                  </span>
                  <p className="empty-state__title">{t('ui_no_recent_alerts')}</p>
                </div>
              ) : (
                <ul className="home-activity__list">
                  {recentAlerts.map((log, index) => (
                    <li key={log.id ?? `${log.ts}-${index}`} className="home-activity__row">
                      <span
                        className={`home-activity__level ${
                          (log.level || log.severity || '').toUpperCase() === 'ERROR'
                            ? 'home-activity__level--error'
                            : 'home-activity__level--warn'
                        }`}
                      >
                        {(log.level || log.severity || '').toUpperCase() === 'ERROR' ? (
                          <AlertTriangle size={12} aria-hidden="true" />
                        ) : null}
                        {(log.level || log.severity || 'INFO').toUpperCase()}
                      </span>
                      <span className="home-activity__text" dir="ltr">
                        {(log.detail || log.message || log.error_name || log.category || '').slice(
                          0,
                          140,
                        )}
                      </span>
                      <span className="home-activity__time">
                        {new Date(log.ts * 1000).toLocaleTimeString(undefined, {
                          hour: '2-digit',
                          minute: '2-digit',
                        })}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>
        )}
      </div>
    </div>
  )
}

export default HomePanel
