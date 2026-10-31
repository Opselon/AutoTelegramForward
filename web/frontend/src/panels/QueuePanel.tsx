/**
 * The delivery queue and the dead-letter queue.
 */
import { Activity, Clock, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { Button, EmptyState } from '../components/ui/Primitives'
import type { DashboardCtx } from '../App'

export default function QueuePanel({ d }: { d: DashboardCtx }) {
  const {
    queueJobs,
    dlqJobs,
    deliveryStats,
    t,
    handleRetryDLQ,
    handlePurgeDLQ,
    openRuleModal,
  } = d
  return (
    <>
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
                  <div>
                    <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>
                      {t('ui_delivery_jobs_dead_letter_queue_dlq')}
                    </h2>
                    <p style={{ fontSize: '0.85rem', color: 'var(--color-text-muted)', marginTop: 4 }}>
                      {t('ui_track_in_flight_delivery_jobs_and_recover_failed_telegram_messag')}
                    </p>
                  </div>
                  {dlqJobs.length > 0 && (
                    <button onClick={handlePurgeDLQ} className="btn btn-danger">
                      <Trash2 size={16} />
                      <span>{t('ui_purge_all_dlq')}</span>
                    </button>
                  )}
                </div>

                {/* Delivery totals — the same snapshot the overview cards read, shown
                    here so the queue tab answers "how much moved?" at a glance. */}
                {deliveryStats && (
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
                      gap: 12,
                      marginBottom: 20,
                    }}
                  >
                    {[
                      { label: t('ui_processed'), value: deliveryStats.processed_total ?? 0, color: 'var(--color-accent-violet)' },
                      { label: t('ui_forwarded'), value: deliveryStats.forwarded_total ?? 0, color: 'var(--color-success)' },
                      { label: t('ui_failed'), value: deliveryStats.failed_total ?? 0, color: 'var(--color-danger)' },
                      { label: t('ui_filtered'), value: deliveryStats.filtered_total ?? 0, color: 'var(--color-warning)' },
                      { label: t('ui_in_queue'), value: deliveryStats.in_queue ?? 0, color: 'var(--color-info)' },
                    ].map((m) => (
                      <div key={m.label} className="glass-panel" style={{ padding: '10px 14px' }}>
                        <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>{m.label}</div>
                        <div style={{ fontSize: '1.15rem', fontWeight: 800, color: m.color }}>{m.value}</div>
                      </div>
                    ))}
                  </div>
                )}

                <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))', gap: 24 }}>
                  {/* Active Delivery Queue */}
                  <div className="glass-panel" style={{ padding: 20 }}>
                    <h3 style={{ fontSize: 'var(--type-md)', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                      <Clock size={16} color="var(--color-accent-violet)" />
                      <span>{t('ui_recent_delivery_jobs')}</span>
                    </h3>

                    {queueJobs.length === 0 ? (
                      <EmptyState
                        title={t('ui_empty_title')}
                        description={t('ui_no_pending_jobs_in_queue')}
                        action={
                          <Button variant="primary" onClick={() => openRuleModal(null)}>
                            <Plus size={16} aria-hidden="true" />
                            <span>{t('ui_create_first_task')}</span>
                          </Button>
                        }
                      />
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                        {(Array.isArray(queueJobs) ? queueJobs : []).map((q) => (
                          <div
                            key={q.id}
                            style={{
                              background: 'var(--color-surface-inset)',
                              padding: '10px 14px',
                              borderRadius: 8,
                              fontSize: '0.8rem',
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center',
                            }}
                          >
                            <div>
                              <div style={{ fontWeight: 600 }}>{t('ui_msg')} #{q.source_message_id}</div>
                              <div style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)' }}>{t('ui_stage')} {q.delivery_stage}</div>
                            </div>
                            <span className="badge badge-direct">{q.status}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Dead Letter Queue */}
                  <div className="glass-panel" style={{ padding: 20 }}>
                    <h3 style={{ fontSize: 'var(--type-md)', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                      <Activity size={16} color="var(--color-danger)" />
                      <span>{t('ui_dead_letter_queue_dlq')}</span>
                      <span className="badge badge-vip">{dlqJobs.length}</span>
                    </h3>

                    {dlqJobs.length === 0 ? (
                      <EmptyState title={t('ui_empty_title')} description={t('ui_dead_letter_queue_is_empty')} />
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                        {(Array.isArray(dlqJobs) ? dlqJobs : []).map((dlq) => (
                          <div
                            key={dlq.id}
                            style={{
                              background: 'var(--color-surface-inset)',
                              padding: '12px 14px',
                              borderRadius: 8,
                              fontSize: '0.8rem',
                              border: '1px solid color-mix(in oklab, var(--color-danger) 35%, transparent)',
                            }}
                          >
                            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                              <span style={{ fontWeight: 700, color: 'var(--color-danger-text)' }}>{dlq.error_category}</span>
                              <button
                                onClick={() => handleRetryDLQ(dlq.id)}
                                className="btn btn-secondary"
                                style={{ padding: '2px 8px', fontSize: 'var(--type-xs)' }}
                              >
                                <RefreshCw size={12} />
                                <span>{t('ui_retry')}</span>
                              </button>
                            </div>
                            <div style={{ color: 'var(--color-text)', fontSize: 'var(--type-xs)' }}>{dlq.last_error}</div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>

    </>
  )
}
