/**
 * MTProto sessions: connect, inspect, terminate.
 */
import { Layers, Users, X } from 'lucide-react'
import { EmptyState, PageHeader, StatusIndicator } from '../components/ui/Primitives'
import type { DashboardCtx } from '../App'

export default function SessionsPanel({ d }: { d: DashboardCtx }) {
  const {
    sessions,
    t,
    handleBackupSession,
    handleTerminateSession,
  } = d
  return (
    <>
              <div>
                <PageHeader
                  title={t('ui_telegram_mtproto_client_sessions')}
                  description={t('ui_monitor_connected_userbot_sessions_auth_status_and_proxies')}
                />

                <div className="sessions-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 20 }}>
                  {(Array.isArray(sessions) ? sessions : []).length === 0 && (
                    <div style={{ gridColumn: '1 / -1' }}>
                      <EmptyState title={t('ui_empty_title')} description={t('ui_empty_body')} />
                    </div>
                  )}
                  {(Array.isArray(sessions) ? sessions : []).map((sess) => (
                    <div key={sess.id} className="glass-panel" style={{ padding: 20 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, gap: 8 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                          <Users size={18} color="var(--color-success)" />
                          <span style={{ fontWeight: 700, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {sess.first_name || sess.username || sess.id}
                          </span>
                        </div>
                        {/* Connection and authorization are two different truths:
                            a session can be online but unauthorized (needs a
                            re-login), so the indicator reads both. */}
                        <StatusIndicator
                          tone={sess.is_authorized ? 'success' : sess.is_active ? 'warning' : 'neutral'}
                          pulse={sess.is_active && sess.is_authorized}
                          label={
                            sess.is_authorized
                              ? t('ui_authorized')
                              : sess.is_active
                              ? t('ui_online')
                              : t('ui_offline')
                          }
                        />
                      </div>

                      <div style={{ fontSize: '0.8rem', color: 'var(--color-text)', display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 14 }}>
                        <div>
                          <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_phone')}</span>
                          <span>{sess.phone_number || (t('ui_n_a'))}</span>
                        </div>
                        <div>
                          <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_user_id')}</span>
                          <span style={{ fontFamily: 'monospace' }}>{sess.user_id}</span>
                        </div>
                        <div>
                          <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_auth_status')}</span>
                          <span style={{ color: sess.is_authorized ? 'var(--color-success)' : 'var(--color-danger)' }}>
                            {sess.is_authorized ? (t('ui_authorized')) : (t('ui_unauthorized'))}
                          </span>
                        </div>
                        <div>
                          <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_proxy')}</span>
                          <span>{sess.proxy || (t('ui_direct_no_proxy'))}</span>
                        </div>
                      </div>

                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', borderTop: '1px solid var(--color-border)', paddingTop: 12 }}>
                        <button
                          onClick={() => handleBackupSession(sess)}
                          className="btn btn-secondary"
                          style={{ flex: 1, fontSize: '0.75rem', padding: '6px 8px', minWidth: 110 }}
                        >
                          <Layers size={13} />
                          <span>{t('ui_backup')}</span>
                        </button>
                        <button
                          onClick={() => handleTerminateSession(sess)}
                          className="btn btn-danger"
                          style={{ flex: 1, fontSize: '0.75rem', padding: '6px 8px', minWidth: 110 }}
                        >
                          <X size={13} />
                          <span>{t('ui_terminate')}</span>
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

    </>
  )
}
